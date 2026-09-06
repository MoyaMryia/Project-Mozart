#!/usr/bin/env python3
"""Offline lifetime tests of extracted XUDC functions, not hardware emulation."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_u_audio_cleanup import ROOT, extract_function


STUBS = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef uint32_t u32;
#define likely(x) (x)
#define dev_dbg(dev, ...) ((void)(dev))
#define dev_err(dev, ...) ((void)(dev))
#define dev_info(dev, ...) ((void)(dev))
#define BIT(n) (1U << (n))
#define EP_STATE_DISABLED 0
#define EP_STATE_RUNNING 1
#define DATA_STAGE_XFER 1
#define USB_STATE_CONFIGURED 2
#define USB_STATE_ADDRESS 1
#define EP_STOPPED 0
#define CTRL 1
#define ST 2
#define CTRL_RUN 1
#define ST_RC 2

struct list_head { struct list_head *next, *prev; };
static void list_init(struct list_head *h) { h->next = h->prev = h; }
static bool list_empty(const struct list_head *h) { return h->next == h; }
static void list_add_tail(struct list_head *n, struct list_head *h) {
    n->prev = h->prev; n->next = h; h->prev->next = n; h->prev = n;
}
static void list_del_init(struct list_head *n) {
    n->prev->next = n->next; n->next->prev = n->prev; list_init(n);
}
#define container_of(p, type, member) ((type *)((char *)(p) - offsetof(type, member)))
#define list_first_entry(h, type, member) container_of((h)->next, type, member)

struct tegra_xudc_ep;
struct usb_ep { struct tegra_xudc_ep *owner; };
struct usb_request {
    int status;
    void (*complete)(struct usb_ep *, struct usb_request *);
};
struct tegra_xudc_request {
    struct usb_request usb_req;
    struct list_head list;
    bool unmapped;
};
struct usb_endpoint_descriptor { bool control, in, isoc; };
struct context { int state; unsigned ring_cookie; };
struct usb_gadget { struct tegra_xudc_ep *owner; int state; };
struct tegra_xudc {
    pthread_mutex_t lock;
    void *dev;
    struct usb_gadget gadget;
    int setup_state, nr_enabled_eps, nr_isoch_eps, device_state;
    u32 regs[3];
    unsigned reloads, unpauses, unhalts, writes;
};
struct tegra_xudc_ep {
    struct tegra_xudc *xudc;
    struct usb_ep usb_ep;
    struct list_head queue;
    const struct usb_endpoint_descriptor *desc, *comp_desc;
    struct context *context;
    unsigned index, givebacks;
    bool disabling;
};
#define usb_endpoint_xfer_control(d) ((d)->control)
#define usb_endpoint_dir_in(d) ((d)->in)
#define usb_endpoint_xfer_isoc(d) ((d)->isoc)
#define ep_ctx_read_state(c) ((c)->state)
#define ep_ctx_write_state(c, s) ((c)->state = (s))

static _Thread_local unsigned lock_depth;
static struct timespec deadline(void) {
    struct timespec t;
    assert(clock_gettime(CLOCK_REALTIME, &t) == 0);
    t.tv_sec += 5;
    return t;
}
static void spin_lock(pthread_mutex_t *m) {
    struct timespec t = deadline();
    assert(lock_depth == 0);
    assert(pthread_mutex_timedlock(m, &t) == 0);
    lock_depth++;
}
static void spin_unlock(pthread_mutex_t *m) {
    assert(lock_depth == 1);
    lock_depth--;
    assert(pthread_mutex_unlock(m) == 0);
}

/* MMIO/context operations record side effects, without touching hardware. */
static int reload_error;
static int ep_reload(struct tegra_xudc *x, unsigned i) {
    assert(lock_depth == 1 && i == x->gadget.owner->index);
    assert(x->gadget.owner->context->state == EP_STATE_DISABLED);
    x->reloads++;
    return reload_error;
}
static void ep_unpause(struct tegra_xudc *x, unsigned i) { (void)i; x->unpauses++; }
static void ep_unhalt(struct tegra_xudc *x, unsigned i) { (void)i; x->unhalts++; }
static u32 xudc_readl(struct tegra_xudc *x, unsigned reg) { return x->regs[reg]; }
static void xudc_writel(struct tegra_xudc *x, u32 val, unsigned reg) {
    x->writes++;
    if (reg == CTRL) x->regs[reg] = val;
    else x->regs[reg] &= ~val;
}
static void usb_gadget_set_state(struct usb_gadget *g, int state) { g->state = state; }
static unsigned callbacks, freed, unmapped, drain_callbacks, peak_givebacks;
static void usb_gadget_unmap_request(struct usb_gadget *g, struct usb_request *u,
                                     bool in) {
    struct tegra_xudc_request *r = container_of(u, struct tegra_xudc_request, usb_req);
    assert(lock_depth == 1 && list_empty(&r->list) && !r->unmapped);
    assert(in == (g->owner->desc->control ?
                  g->owner->xudc->setup_state == DATA_STAGE_XFER : g->owner->desc->in));
    r->unmapped = true;
    unmapped++;
}
static void usb_gadget_giveback_request(struct usb_ep *ep, struct usb_request *u) {
    assert(lock_depth == 0);
    callbacks++;
    u->complete(ep, u); /* The callback may free u. Do not inspect it afterward. */
}
'''


HARNESS = r'''
static pthread_mutex_t event_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t event_cond = PTHREAD_COND_INITIALIZER;
static bool entered, release_callback;

static void free_request(struct usb_request *u) {
    struct tegra_xudc_request *r = container_of(u, struct tegra_xudc_request, usb_req);
    assert(r->unmapped && list_empty(&r->list));
    freed++;
    free(r);
}
static struct tegra_xudc_request *request(struct tegra_xudc_ep *ep,
        void (*complete)(struct usb_ep *, struct usb_request *)) {
    struct tegra_xudc_request *r = calloc(1, sizeof(*r));
    assert(r);
    r->usb_req.status = -EINPROGRESS;
    r->usb_req.complete = complete;
    list_add_tail(&r->list, &ep->queue);
    return r;
}
static void drain_callback(struct usb_ep *usb_ep, struct usb_request *u) {
    struct tegra_xudc_ep *ep = usb_ep->owner;
    spin_lock(&ep->xudc->lock);
    assert(ep->givebacks == 1 && ep->disabling && ep->desc);
    assert(ep->context->state == EP_STATE_DISABLED && ep->xudc->reloads == 1);
    assert(u->status == -ESHUTDOWN);
    /* The real queue entry point's disabling guard is checked in Python. */
    assert(__tegra_xudc_ep_disable(ep) == -EBUSY);
    drain_callbacks++;
    spin_unlock(&ep->xudc->lock);
    free_request(u);
}
static void blocked_callback(struct usb_ep *usb_ep, struct usb_request *u) {
    struct tegra_xudc_ep *ep = usb_ep->owner;
    spin_lock(&ep->xudc->lock);
    assert(ep->givebacks == 1 && !ep->disabling && u->status == 0);
    spin_unlock(&ep->xudc->lock);
    assert(pthread_mutex_lock(&event_lock) == 0);
    entered = true;
    assert(pthread_cond_broadcast(&event_cond) == 0);
    struct timespec t = deadline();
    while (!release_callback)
        assert(pthread_cond_timedwait(&event_cond, &event_lock, &t) == 0);
    assert(pthread_mutex_unlock(&event_lock) == 0);
    free_request(u);
}
static void nested_inner(struct usb_ep *usb_ep, struct usb_request *u) {
    struct tegra_xudc_ep *ep = usb_ep->owner;
    spin_lock(&ep->xudc->lock);
    assert(ep->givebacks == 2 && !ep->disabling);
    assert(u->status == -ECONNRESET); /* Preserve an existing completion error. */
    peak_givebacks = ep->givebacks;
    assert(__tegra_xudc_ep_disable(ep) == -EBUSY);
    spin_unlock(&ep->xudc->lock);
    free_request(u);
}
static void nested_outer(struct usb_ep *usb_ep, struct usb_request *u) {
    struct tegra_xudc_ep *ep = usb_ep->owner;
    spin_lock(&ep->xudc->lock);
    assert(ep->givebacks == 1 && u->status == 0);
    struct tegra_xudc_request *inner = request(ep, nested_inner);
    inner->usb_req.status = -ECONNRESET;
    tegra_xudc_req_done(ep, inner, 0);
    assert(ep->givebacks == 1 && freed == 1);
    assert(__tegra_xudc_ep_disable(ep) == -EBUSY);
    spin_unlock(&ep->xudc->lock);
    free_request(u);
}
static void *complete_thread(void *arg) {
    struct tegra_xudc_ep *ep = arg;
    spin_lock(&ep->xudc->lock);
    struct tegra_xudc_request *r = list_first_entry(&ep->queue,
                                                  struct tegra_xudc_request, list);
    tegra_xudc_req_done(ep, r, 0);
    assert(ep->givebacks == 0 && lock_depth == 1);
    spin_unlock(&ep->xudc->lock);
    return NULL;
}

int main(int argc, char **argv) {
    assert(argc == 2);
    struct tegra_xudc x = { .nr_enabled_eps = 2, .nr_isoch_eps = 1,
                            .device_state = USB_STATE_CONFIGURED };
    struct context ctx = { .state = EP_STATE_RUNNING, .ring_cookie = 0x1234 };
    struct usb_endpoint_descriptor desc = { .in = true, .isoc = true };
    struct tegra_xudc_ep ep = { .xudc = &x, .context = &ctx, .desc = &desc,
                               .comp_desc = &desc, .index = 2 };
    ep.usb_ep.owner = &ep;
    x.gadget.owner = &ep;
    x.regs[EP_STOPPED] = BIT(ep.index);
    x.regs[CTRL] = CTRL_RUN;
    x.regs[ST] = ST_RC;
    list_init(&ep.queue);
    assert(pthread_mutex_init(&x.lock, NULL) == 0);

    if (strcmp(argv[1], "timeout") == 0) {
        struct tegra_xudc_request *r = request(&ep, drain_callback);
        reload_error = -ETIMEDOUT;
        spin_lock(&x.lock);
        assert(__tegra_xudc_ep_disable(&ep) == -ETIMEDOUT);
        assert(ep.disabling && ep.desc == &desc && ep.comp_desc == &desc);
        assert(!list_empty(&ep.queue) && !callbacks && !freed && !unmapped);
        assert(x.nr_enabled_eps == 2 && x.nr_isoch_eps == 1);
        assert(x.reloads == 1 && !x.unpauses && !x.unhalts && !x.writes);
        assert(ctx.ring_cookie == 0x1234);
        assert(__tegra_xudc_ep_disable(&ep) == -EBUSY);
        assert(x.reloads == 1);
        spin_unlock(&x.lock);
        /* Harness cleanup only: real failure retains DMA objects until reboot. */
        list_del_init(&r->list);
        free(r);
        assert(pthread_mutex_destroy(&x.lock) == 0);
        return 0;
    } else if (strcmp(argv[1], "blocked") == 0) {
        request(&ep, blocked_callback);
        request(&ep, drain_callback);
        request(&ep, drain_callback);
        pthread_t thread;
        assert(pthread_create(&thread, NULL, complete_thread, &ep) == 0);
        assert(pthread_mutex_lock(&event_lock) == 0);
        struct timespec t = deadline();
        int wait_result = 0;
        while (!entered && !wait_result)
            wait_result = pthread_cond_timedwait(&event_cond, &event_lock, &t);
        bool ok = entered && wait_result == 0;
        assert(pthread_mutex_unlock(&event_lock) == 0);

        if (ok) {
            spin_lock(&x.lock);
            ok = ep.givebacks == 1 && callbacks == 1 && freed == 0;
            int ret = __tegra_xudc_ep_disable(&ep);
            ok = ok && ret == -EBUSY && freed == 0 && unmapped == 1 &&
                 !ep.disabling && ep.desc == &desc && ep.comp_desc == &desc &&
                 ctx.state == EP_STATE_RUNNING && ctx.ring_cookie == 0x1234 &&
                 x.reloads == 0 && x.writes == 0 && x.unpauses == 0 &&
                 x.unhalts == 0 && x.nr_enabled_eps == 2 && x.nr_isoch_eps == 1;
            spin_unlock(&x.lock);
        }
        /* Release and join even if the observations above failed. */
        assert(pthread_mutex_lock(&event_lock) == 0);
        release_callback = true;
        assert(pthread_cond_broadcast(&event_cond) == 0);
        assert(pthread_mutex_unlock(&event_lock) == 0);
        t = deadline();
        assert(pthread_timedjoin_np(thread, NULL, &t) == 0);
        assert(ok && freed == 1 && ep.givebacks == 0);
        spin_lock(&x.lock);
        assert(__tegra_xudc_ep_disable(&ep) == 0);
        assert(callbacks == 3 && freed == 3 && unmapped == 3 && drain_callbacks == 2);
    } else if (strcmp(argv[1], "nested") == 0) {
        /* Exercise the control-endpoint unmap direction branch too. */
        desc.control = true;
        x.setup_state = DATA_STAGE_XFER;
        struct tegra_xudc_request *r = request(&ep, nested_outer);
        spin_lock(&x.lock);
        tegra_xudc_req_done(&ep, r, 0);
        assert(ep.givebacks == 0 && peak_givebacks == 2);
        assert(callbacks == 2 && freed == 2 && unmapped == 2);
        assert(__tegra_xudc_ep_disable(&ep) == 0);
    } else {
        assert(strcmp(argv[1], "disabling") == 0);
        spin_lock(&x.lock);
        ep.disabling = true;
        assert(__tegra_xudc_ep_disable(&ep) == -EBUSY);
        assert(ctx.state == EP_STATE_RUNNING && x.reloads == 0 && freed == 0);
        ep.disabling = false;
        request(&ep, drain_callback);
        assert(__tegra_xudc_ep_disable(&ep) == 0);
        assert(callbacks == 1 && freed == 1 && unmapped == 1 && drain_callbacks == 1);
    }
    assert(lock_depth == 1 && ep.givebacks == 0 && !ep.disabling);
    assert(list_empty(&ep.queue) && !ep.desc && !ep.comp_desc);
    assert(ctx.state == EP_STATE_DISABLED && ctx.ring_cookie == 0);
    assert(x.nr_enabled_eps == 1 && x.nr_isoch_eps == 0);
    assert(x.device_state == USB_STATE_ADDRESS && x.gadget.state == USB_STATE_ADDRESS);
    assert(x.reloads == 1 && x.unpauses == 1 && x.unhalts == 1 && x.writes == 3);
    assert(!x.regs[CTRL] && !x.regs[ST] && !x.regs[EP_STOPPED]);
    assert(__tegra_xudc_ep_disable(&ep) == -EINVAL);
    assert(x.reloads == 1 && x.writes == 3 && x.nr_enabled_eps == 1);
    spin_unlock(&x.lock);
    assert(pthread_mutex_destroy(&x.lock) == 0);
    assert(pthread_cond_destroy(&event_cond) == 0);
    assert(pthread_mutex_destroy(&event_lock) == 0);
    return 0;
}
'''


class XudcLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "tegra-xudc.c").read_text()
        functions = "\n\n".join(extract_function(cls.source, name) for name in (
            "tegra_xudc_req_done(", "tegra_xudc_ep_nuke(",
            "__tegra_xudc_ep_disable("))
        tmp = tempfile.TemporaryDirectory(prefix="mozart-xudc-lifetime-", dir="/tmp/opencode")
        cls.addClassCleanup(tmp.cleanup)
        cfile = Path(tmp.name) / "lifetime.c"
        cls.binary = Path(tmp.name) / "lifetime"
        cfile.write_text(STUBS + functions + HARNESS)
        result = subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
             "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
             "-fno-omit-frame-pointer", "-g", str(cfile), "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(f"harness compilation failed:\n{result.stdout}{result.stderr}")

    def run_scenario(self, scenario):
        result = subprocess.run(
            [str(self.binary), scenario], capture_output=True, text=True, timeout=20,
            env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
                 "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_blocked_giveback_prevents_disable_then_drains(self):
        self.run_scenario("blocked")

    def test_nested_givebacks_count_and_free_requests(self):
        self.run_scenario("nested")

    def test_disabling_guard_and_drain_callbacks(self):
        self.run_scenario("disabling")

    def test_reload_timeout_retains_requests_and_blocks_reuse(self):
        self.run_scenario("timeout")

    def test_enable_checks_reload_failure(self):
        function = extract_function(self.source, "__tegra_xudc_ep_enable(")
        reload = function.index("ret = ep_reload(")
        self.assertLess(reload, function.index("ep->disabling = true;", reload))
        self.assertLess(function.index("if (ret)\n\t\treturn ret;", reload),
                        function.index("out:"))

    def test_enable_guard_precedes_endpoint_mutation(self):
        function = extract_function(self.source, "__tegra_xudc_ep_enable(")
        guard = "if (ep->givebacks || ep->disabling)"
        self.assertIn(guard, function)
        guarded = function[function.index(guard):]
        self.assertRegex(guarded, r"^if \(ep->givebacks \|\| ep->disabling\)\s+return -EBUSY;")
        for mutation in ("__tegra_xudc_ep_disable(ep)", "ep->desc = desc",
                         "memset(ep->transfer_ring"):
            self.assertLess(function.index(guard), function.index(mutation))

    def test_queue_rejects_disabling_before_enqueue(self):
        function = extract_function(self.source, "\ntegra_xudc_ep_queue(")
        self.assertRegex(function, r"if \([^{};]*ep->disabling[^{};]*\)\s*\{\s*"
                                  r"ret = -ESHUTDOWN;\s*goto unlock;")
        self.assertLess(function.index("spin_lock_irqsave("), function.index("ep->disabling"))
        self.assertLess(function.index("ep->disabling"), function.index("__tegra_xudc_ep_queue("))

    def test_setup_callback_gate_precedes_dispatch(self):
        function = extract_function(self.source, "tegra_xudc_handle_ep0_setup_packet(")
        self.assertRegex(function, r"if \(!xudc->async_callbacks \|\| !xudc->driver\)\s+return;")
        self.assertLess(function.index("!xudc->async_callbacks"),
                        function.index("tegra_xudc_ep0_delegate_req("))


if __name__ == "__main__":
    unittest.main()
