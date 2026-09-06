#!/usr/bin/env python3
"""Offline regression tests for the request ownership rules in u_audio.c."""

import pathlib
import os
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "u_audio.c"


def extract_function(source, name):
    marker = source.index(name)
    start = source.rfind("static", 0, marker)
    brace = source.index("{", marker)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unbalanced function: {name}")


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define GFP_ATOMIC 0
#define ESHUTDOWN 108
#define ENOMEM 12
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define pr_debug(...) ((void)0)
#define dev_err(dev, ...) ((void)(dev))
#define dev_err_ratelimited(dev, ...) ((void)(dev))

struct usb_ep { int unused; };
struct usb_request {
    int status, actual, length;
    void *buf;
    void *context;
    void (*complete)(struct usb_ep *, struct usb_request *);
};
struct uac_params { int req_number; };
struct snd_card { void *dev; };
struct usb_gadget { int speed; };
struct g_audio { struct uac_params params; struct usb_gadget *gadget; struct snd_card *card; struct usb_ep *out_ep; };
struct snd_uac_chip { struct g_audio *audio_dev; struct snd_card *card; };
struct uac_rtd_params {
    struct snd_uac_chip *uac;
    bool ep_enabled, fb_ep_enabled;
    struct usb_request **reqs;
    struct usb_request *req_fback;
    int srate, pitch;
};

static int disable_result;
static int disable_calls, free_calls, queue_calls, kfree_calls;
static bool disabling;
static struct usb_request *queued;

int usb_ep_disable(struct usb_ep *ep) {
    disable_calls++;
    if (disable_result) return disable_result;
    disabling = true;
    if (queued) {
        struct usb_request *req = queued;
        queued = NULL;
        req->status = -ESHUTDOWN;
        req->complete(ep, req);
    }
    disabling = false;
    return 0;
}
void usb_ep_free_request(struct usb_ep *ep, struct usb_request *req) {
    (void)ep; assert(!disabling); free_calls++; free(req);
}
void kfree(void *p) { assert(!disabling); if (p) { kfree_calls++; free(p); } }
int usb_ep_queue(struct usb_ep *ep, struct usb_request *req, int flags) {
    (void)ep; (void)req; (void)flags; assert(!disabling); queue_calls++; return -5;
}
void u_audio_set_fback_frequency(int speed, struct usb_ep *ep,
                                 unsigned long long rate, unsigned int pitch,
                                 void *buf) {
    (void)speed; (void)ep; (void)rate; (void)pitch; (void)buf;
}

'''


def compile_and_run():
    source = SOURCE.read_text()
    functions = "\n\n".join(extract_function(source, name) for name in (
        "free_ep(", "free_ep_fback(", "u_audio_iso_fback_complete("))
    main = r'''
static struct usb_request *request(void) { return calloc(1, sizeof(struct usb_request)); }
static void reset_counters(void) { disable_result = 0; disable_calls = free_calls = queue_calls = kfree_calls = 0; }

int main(void) {
    struct usb_ep ep;
    struct snd_card card = {0};
    struct usb_gadget gadget = {0};
    struct g_audio audio = { .gadget = &gadget, .card = &card, .out_ep = &ep };
    struct snd_uac_chip uac = { .audio_dev = &audio, .card = &card };
    struct uac_rtd_params p = { .uac = &uac };
    struct usb_request *reqs[3] = { request(), NULL, request() };
    audio.params.req_number = 3; p.reqs = reqs; p.ep_enabled = true;

    disable_result = -5;
    assert(free_ep(&p, &ep) == -5 && !p.ep_enabled && free_calls == 0);
    assert(reqs[0] && reqs[2]); /* failed disable retains ownership */
    disable_result = 0;
    assert(free_ep(&p, &ep) == 0 && free_calls == 2 && !reqs[0] && !reqs[2]);
    assert(free_ep(&p, &ep) == 0 && free_calls == 2); /* retry is idempotent */

    p.req_fback = request(); p.req_fback->buf = malloc(8); p.fb_ep_enabled = true;
    p.req_fback->context = &p;
    p.req_fback->complete = u_audio_iso_fback_complete;
    queued = p.req_fback;
    disable_result = -6;
    assert(free_ep_fback(&p, &ep) == -6 && !p.fb_ep_enabled && free_calls == 2);
    assert(p.req_fback && kfree_calls == 0);
    disable_result = 0;
    assert(free_ep_fback(&p, &ep) == 0 && !p.req_fback && free_calls == 3 && kfree_calls == 1);
    assert(free_ep_fback(&p, &ep) == 0 && free_calls == 3 && kfree_calls == 1);

    reset_counters();
    p.fb_ep_enabled = false; p.req_fback = request(); p.req_fback->buf = malloc(4);
    p.req_fback->complete = u_audio_iso_fback_complete;
    p.req_fback->context = &p; p.req_fback->status = 0;
    u_audio_iso_fback_complete(&ep, p.req_fback);
    assert(queue_calls == 0 && free_calls == 0); /* stop callback neither frees nor requeues */
    free(p.req_fback->buf); free(p.req_fback); p.req_fback = NULL;

    p.fb_ep_enabled = true; p.req_fback = request(); p.req_fback->buf = malloc(4);
    p.req_fback->context = &p; p.req_fback->status = -ESHUTDOWN;
    u_audio_iso_fback_complete(&ep, p.req_fback);
    assert(queue_calls == 0 && free_calls == 0);
    free(p.req_fback->buf); free(p.req_fback);

    reset_counters();
    /* A failed completion requeue leaves ownership with the stop path. */
    p.req_fback = request(); p.req_fback->buf = malloc(4);
    p.req_fback->context = &p; p.fb_ep_enabled = true;
    u_audio_iso_fback_complete(&ep, p.req_fback);
    assert(queue_calls == 1 && free_calls == 0 && kfree_calls == 0);
    assert(free_ep_fback(&p, &ep) == 0);
    assert(free_calls == 1 && kfree_calls == 1);

    reset_counters();
    /* Request allocated, but feedback-buffer allocation failed. */
    p.req_fback = request(); p.fb_ep_enabled = true;
    assert(free_ep_fback(&p, &ep) == 0);
    assert(free_calls == 1 && kfree_calls == 0 && !p.req_fback);
    assert(free_ep(&p, NULL) == 0 && free_ep_fback(&p, NULL) == 0);
    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix="mozart-u-audio-", dir="/tmp/opencode") as tmp:
        cfile = pathlib.Path(tmp) / "harness.c"
        binary = pathlib.Path(tmp) / "harness"
        cfile.write_text(HARNESS + functions + main)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-fsanitize=address,undefined",
                        "-fno-omit-frame-pointer", str(cfile), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True,
                       env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1"})


class UAudioCleanupTests(unittest.TestCase):
    def test_exact_source_functions(self):
        compile_and_run()

    def test_data_callback_retains_owner(self):
        callback = extract_function(SOURCE.read_text(), "u_audio_iso_complete(")
        self.assertNotIn("usb_ep_free_request(", callback)
        self.assertNotIn("kfree(", callback)
        self.assertIn("READ_ONCE(prm->ep_enabled)", callback)

    def test_stop_does_not_dequeue(self):
        for name in ("free_ep(", "free_ep_fback("):
            self.assertNotIn("usb_ep_dequeue(", extract_function(SOURCE.read_text(), name))


if __name__ == "__main__":
    unittest.main()
