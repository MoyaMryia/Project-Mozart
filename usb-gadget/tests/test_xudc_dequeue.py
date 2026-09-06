#!/usr/bin/env python3
"""Exercise the real dequeue function with a mocked transfer ring, offline."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_u_audio_cleanup import ROOT, extract_function


STUBS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>
#include <errno.h>
typedef unsigned long dma_addr_t;
#define EP_STATE_RUNNING 1
#define dma_mapping_error(dev, addr) 0
struct tegra_xudc { int unused; };
struct tegra_xudc_trb { int cycle; };
struct context { int state, dcs, edtla, partial, offset; dma_addr_t deq; };
struct tegra_xudc_request {
    struct { unsigned actual; } usb_req;
    bool trbs_queued;
    struct tegra_xudc_request *next;
};
struct tegra_xudc_ep {
    struct tegra_xudc *xudc;
    struct tegra_xudc_request *queue;
    struct context *context;
    struct tegra_xudc_trb transfer_ring[2];
    int index, enq_ptr, pcs;
};
#define list_for_each_entry(it, queue_ptr, member) \
    for ((it) = *(queue_ptr); (it); (it) = (it)->next)
#define ep_ctx_read_state(c) ((c)->state)
#define ep_ctx_read_deq_ptr(c) ((c)->deq)
#define ep_ctx_read_dcs(c) ((c)->dcs)
#define ep_ctx_read_edtla(c) ((c)->edtla)
#define ep_ctx_write_edtla(c, v) ((c)->edtla = (v))
#define ep_ctx_write_partial_td(c, v) ((c)->partial = (v))
#define ep_ctx_write_data_offset(c, v) ((c)->offset = (v))
#define ep_ctx_write_deq_ptr(c, v) ((c)->deq = (v))
#define ep_ctx_write_dcs(c, v) ((c)->dcs = (v))
#define trb_read_cycle(t) ((t)->cycle)
static int branch, completed, squeezed, reloads, kicks;
static bool free_on_callback;
static void ep_pause(struct tegra_xudc *x, int i) { (void)x; (void)i; }
static void ep_wait_for_inactive(struct tegra_xudc *x, int i) { (void)x; (void)i; }
static void ep_unpause(struct tegra_xudc *x, int i) { (void)x; (void)i; }
static void ep_reload(struct tegra_xudc *x, int i) { (void)x; (void)i; reloads++; }
static struct tegra_xudc_trb *trb_phys_to_virt(struct tegra_xudc_ep *ep, dma_addr_t p) {
    (void)p; return &ep->transfer_ring[0];
}
static dma_addr_t trb_virt_to_phys(struct tegra_xudc_ep *ep, struct tegra_xudc_trb *p) {
    (void)ep; (void)p; return 1234;
}
static bool trb_in_request(struct tegra_xudc_ep *ep, struct tegra_xudc_request *r,
                           struct tegra_xudc_trb *t) {
    (void)ep; (void)r; (void)t; return branch == 1;
}
static bool trb_before_request(struct tegra_xudc_ep *ep, struct tegra_xudc_request *r,
                               struct tegra_xudc_trb *t) {
    (void)ep; (void)r; (void)t; return branch == 2;
}
static void squeeze_transfer_ring(struct tegra_xudc_ep *ep, struct tegra_xudc_request *r) {
    (void)r; squeezed++; ep->enq_ptr = 1;
}
static void tegra_xudc_ep_kick_queue(struct tegra_xudc_ep *ep) { (void)ep; kicks++; }
static void tegra_xudc_req_done(struct tegra_xudc_ep *ep,
                              struct tegra_xudc_request *r, int status) {
    assert(status == -ECONNRESET);
    if (branch == 1 && r->trbs_queued && r->usb_req.actual) {
        assert(ep->context->edtla == 0 && ep->context->partial == 0);
        assert(ep->context->offset == 0 && ep->context->deq == 1234);
        assert(reloads == 1);
    }
    completed++;
    ep->queue = NULL;
    if (free_on_callback) free(r);
    else { r->usb_req.actual = 0; ep->queue = r; }
}
'''

MAIN = r'''
int main(void) {
    for (int mode = 0; mode < 2; mode++) {
        for (branch = 0; branch < 4; branch++) {
            struct tegra_xudc x = {0};
            struct context ctx = { .state = 1, .dcs = 1, .edtla = 8,
                                   .partial = 1, .offset = 4 };
            struct tegra_xudc_request *r = calloc(1, sizeof(*r));
            struct tegra_xudc_ep ep = { .xudc = &x, .context = &ctx,
                                        .queue = r, .pcs = 1 };
            ep.transfer_ring[0].cycle = 1;
            r->trbs_queued = branch != 0;
            completed = squeezed = reloads = kicks = 0;
            free_on_callback = mode == 0;
            assert(__tegra_xudc_ep_dequeue(&ep, r) == 0);
            assert(completed == 1);
            assert(squeezed == (branch == 1 || branch == 2));
            assert(kicks == (branch == 1 || branch == 2));
            if (!free_on_callback) { assert(ep.queue == r); free(r); }
        }
    }
    struct tegra_xudc x = {0};
    struct tegra_xudc_ep ep = { .xudc = &x };
    struct tegra_xudc_request r = {0};
    completed = 0;
    assert(__tegra_xudc_ep_dequeue(&ep, &r) == -EINVAL);
    assert(completed == 0);
    return 0;
}
'''


class XudcDequeueTests(unittest.TestCase):
    def run_dequeue(self, source):
        function = extract_function(source, "__tegra_xudc_ep_dequeue(")
        with tempfile.TemporaryDirectory(prefix="mozart-dequeue-", dir="/tmp/opencode") as tmp:
            cfile, binary = Path(tmp) / "test.c", Path(tmp) / "test"
            cfile.write_text(STUBS + function + MAIN)
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            str(cfile), "-o", str(binary)], check=True)
            return subprocess.run([str(binary)], capture_output=True, text=True,
                                  env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1"})

    def test_actual_dequeue_with_free_and_requeue_callbacks(self):
        result = self.run_dequeue((ROOT / "tegra-xudc.c").read_text())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_crash_baseline_fails_regression(self):
        baseline = ROOT / "crash-20260905-serial-test" / "tegra-xudc.c"
        result = self.run_dequeue(baseline.read_text())
        self.assertNotEqual(result.returncode, 0)
        # The old driver calls back before completing its ring-context updates.
        self.assertIn("Assertion", result.stderr)


if __name__ == "__main__":
    unittest.main()
