"""Offline guard tests; no hardware or kernel module operations."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_u_audio_cleanup import ROOT, extract_function


def run_c(code):
    with tempfile.TemporaryDirectory(dir="/tmp/opencode", prefix="mozart-guards-") as tmp:
        source, binary = Path(tmp) / "test.c", Path(tmp) / "test"
        source.write_text(code)
        subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                        str(source), "-o", str(binary)], check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=15,
                       env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1"})


class GuardTests(unittest.TestCase):
    def test_xudc_does_not_advertise_unsupported_iso(self):
        source = (ROOT / "tegra-xudc.c").read_text()
        alloc = extract_function(source, "tegra_xudc_alloc_ep(")
        self.assertIn("ep->usb_ep.caps.type_bulk = true;", alloc)
        self.assertIn("ep->usb_ep.caps.type_int = true;", alloc)
        self.assertNotIn("caps.type_iso = true", alloc)

    def test_ring_address_bounds(self):
        function = extract_function((ROOT / "tegra-xudc.c").read_text(), "trb_phys_to_virt(")
        run_c(r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
typedef uint64_t dma_addr_t;
#define XUDC_TRANSFER_RING_SIZE 64
struct tegra_xudc_trb { uint32_t words[4]; };
struct tegra_xudc_ep { dma_addr_t transfer_ring_phys; struct tegra_xudc_trb *transfer_ring; };
''' + function + r'''
int main(void) {
    struct tegra_xudc_trb ring[64];
    struct tegra_xudc_ep ep = { .transfer_ring_phys = 0x123400000ULL, .transfer_ring = ring };
    for (unsigned i = 0; i < 64; i++)
        assert(trb_phys_to_virt(&ep, ep.transfer_ring_phys + 16 * i) == &ring[i]);
    assert(!trb_phys_to_virt(&ep, ep.transfer_ring_phys - 16));
    assert(!trb_phys_to_virt(&ep, ep.transfer_ring_phys + 1024));
    assert(!trb_phys_to_virt(&ep, ep.transfer_ring_phys + 1));
    assert(!trb_phys_to_virt(&ep, ep.transfer_ring_phys + (1ULL << 36)));
    assert(!trb_phys_to_virt(&ep, UINT64_MAX));
    return 0;
}
''')

    def test_quarantine_cleanup(self):
        source = (ROOT / "u_audio.c").read_text()
        start = source.index("void g_audio_cleanup(")
        end = source.index("EXPORT_SYMBOL_GPL(g_audio_cleanup)", start)
        run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#define WRITE_ONCE(x, v) ((x) = (v))
#define THIS_MODULE 0
#define dev_err(dev, ...) ((void)(dev))
struct snd_card { int unused; };
struct prm { bool ep_enabled, fb_ep_enabled; void *reqs, *rbuf; };
struct snd_uac_chip { struct snd_card *card; struct prm c_prm, p_prm; };
struct gadget { int dev; };
struct g_audio { struct snd_uac_chip *uac; struct gadget *gadget; bool quarantined;
                 void *in_ep_fback, *out_ep, *in_ep; };
static int attempt, fail_at, disconnected, card_freed, frees, pins;
static int free_ep_fback(struct prm *p, void *ep) {
    (void)p; (void)ep; assert(disconnected); return ++attempt == fail_at ? -5 : 0;
}
static int free_ep(struct prm *p, void *ep) { return free_ep_fback(p, ep); }
static void snd_card_disconnect(struct snd_card *c) { (void)c; disconnected++; }
static void snd_card_free(struct snd_card *c) { (void)c; assert(attempt == 3); card_freed++; }
static void kfree(void *p) { (void)p; assert(card_freed); frees++; }
static void __module_get(int m) { (void)m; pins++; }
''' + source[start:end] + r'''
int main(void) {
    for (fail_at = 0; fail_at <= 3; fail_at++) {
        struct snd_card card = {0}; struct gadget gadget = {0};
        struct snd_uac_chip uac = { .card = &card };
        struct g_audio audio = { .uac = &uac, .gadget = &gadget };
        attempt = disconnected = card_freed = frees = pins = 0;
        g_audio_cleanup(&audio);
        assert(attempt == 3 && disconnected == 1);
        if (fail_at) {
            assert(audio.quarantined && audio.uac == &uac);
            assert(!card_freed && !frees && pins == 1);
            g_audio_cleanup(&audio);
            assert(attempt == 3 && disconnected == 1 && pins == 1);
        } else {
            assert(!audio.uac && card_freed == 1 && frees == 5 && !pins);
            g_audio_cleanup(&audio);
            assert(frees == 5);
        }
    }
    return 0;
}
''')

    def test_quarantine_blocks_rebind_before_descriptor_changes(self):
        source = (ROOT / "f_uac2.c").read_text()
        bind = extract_function(source, "afunc_bind(")
        self.assertLess(bind.index("READ_ONCE(uac2_quarantined)"),
                        bind.index("afunc_validate_opts("))
        unbind = extract_function(source, "afunc_unbind(")
        self.assertIn("WRITE_ONCE(uac2_quarantined, true)", unbind)
        self.assertLess(unbind.index("agdev->quarantined"),
                        unbind.index("usb_free_all_descriptors("))

    def test_ring_error_does_not_advance_dequeue(self):
        source = (ROOT / "tegra-xudc.c").read_text()
        event = extract_function(source, "tegra_xudc_handle_transfer_event(")
        ring_error = event.index("comp_code == TRB_CMPL_CODE_RING_UNDERRUN")
        self.assertLess(event.index("return;", ring_error),
                        event.index("tegra_xudc_handle_transfer_completion("))
        self.assertNotIn("ep->deq_ptr =", event)
        completion = extract_function(source, "tegra_xudc_handle_transfer_completion(")
        self.assertLess(completion.index("ep->deq_ptr ="),
                        completion.index("tegra_xudc_req_done("))

    def test_completion_residual_and_cancelled_retirement(self):
        function = extract_function((ROOT / "tegra-xudc.c").read_text(),
                                    "tegra_xudc_handle_transfer_completion(")
        run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>
#define XUDC_TRANSFER_RING_SIZE 64
#define TRB_CMPL_CODE_SHORT_PACKET 13
#define dev_err(dev, ...) ((void)(dev))
#define dev_dbg(dev, ...) ((void)(dev))
#define dev_warn_ratelimited(dev, ...) ((void)(dev))
struct tegra_xudc { void *dev; };
struct tegra_xudc_trb { unsigned code, residual, slot; bool chain; };
struct descriptor { bool control; };
struct tegra_xudc_request {
    struct { unsigned length, actual; } usb_req;
    unsigned trbs_needed, trbs_queued;
    struct tegra_xudc_trb *last_trb;
};
struct tegra_xudc_ep {
    unsigned index, deq_ptr;
    bool ring_full;
    struct descriptor *desc;
    struct tegra_xudc_trb transfer_ring[64];
    struct tegra_xudc_request *request;
};
#define trb_read_cmpl_code(e) ((e)->code)
#define trb_read_transfer_len(e) ((e)->residual)
#define trb_read_data_ptr(e) ((e)->slot)
#define trb_read_chain(t) ((t)->chain)
#define usb_endpoint_xfer_control(d) ((d)->control)
static unsigned halts, callbacks, kicks, control_done, expected_deq;
static struct tegra_xudc_trb *trb_phys_to_virt(struct tegra_xudc_ep *ep, unsigned slot) {
    return slot < 64 ? &ep->transfer_ring[slot] : NULL;
}
static struct tegra_xudc_request *trb_to_request(struct tegra_xudc_ep *ep,
                                               struct tegra_xudc_trb *trb) {
    (void)trb; return ep->request;
}
static void ep_halt(struct tegra_xudc *x, unsigned index) { (void)x; (void)index; halts++; }
static void tegra_xudc_req_done(struct tegra_xudc_ep *ep,
                               struct tegra_xudc_request *r, int status) {
    assert(!status && ep->deq_ptr == expected_deq && !ep->ring_full);
    assert(r->usb_req.actual == 180);
    callbacks++;
    free(r); /* Sanitizers catch any post-giveback request dereference. */
    ep->request = NULL;
}
static void tegra_xudc_ep0_req_done(struct tegra_xudc *x) { (void)x; control_done++; }
static void tegra_xudc_ep_kick_queue(struct tegra_xudc_ep *ep) { (void)ep; kicks++; }
''' + function + r'''
int main(void) {
    struct tegra_xudc x = {0};
    struct descriptor desc = {0};
    for (unsigned mode = 0; mode < 8; mode++) {
        struct tegra_xudc_ep ep = { .index = 7, .deq_ptr = 9, .ring_full = true, .desc = &desc };
        struct tegra_xudc_trb event = { .code = 1, .slot = 2, .residual = 12 };
        struct tegra_xudc_request *r = calloc(1, sizeof(*r));
        assert(r);
        r->usb_req.length = 192;
        r->trbs_needed = r->trbs_queued = 1;
        r->last_trb = &ep.transfer_ring[5];
        ep.request = r;
        halts = callbacks = kicks = control_done = 0;
        expected_deq = 3;
        if (mode == 0) ep.request = NULL; /* Cancelled TD's pending event retires slots. */
        if (mode == 1) event.slot = 64; /* Invalid DMA pointer. */
        if (mode == 2) event.slot = 63; /* LINK is never a completion TD. */
        if (mode == 3) event.residual = 193;
        if (mode == 5) { event.code = 13; expected_deq = 6; }
        if (mode == 6) { event.slot = 62; expected_deq = 0; }
        if (mode == 7) { ep.transfer_ring[2].chain = true; r->trbs_needed = 2; }
        tegra_xudc_handle_transfer_completion(&x, &ep, &event);
        if (mode == 0) {
            assert(ep.deq_ptr == 3 && !ep.ring_full && !callbacks && kicks == 1 && !halts);
            free(r);
        } else if (mode < 4) {
            assert(ep.deq_ptr == 9 && ep.ring_full && !callbacks && !kicks);
            assert(halts == (mode != 0));
            free(r);
        } else if (mode == 7) {
            assert(ep.deq_ptr == 3 && !ep.ring_full && !callbacks && kicks == 1);
            free(r);
        } else {
            assert(callbacks == 1 && kicks == 1 && !halts && !control_done);
        }
    }
    return 0;
}
''')

    def test_dma_ownership_ordering(self):
        source = (ROOT / "tegra-xudc.c").read_text()
        publish = extract_function(source, "tegra_xudc_queue_one_trb(")
        self.assertLess(publish.index("trb_write_data_ptr("), publish.index("dma_wmb();"))
        self.assertLess(publish.index("dma_wmb();"), publish.index("trb_write_cycle("))
        consume = extract_function(source, "tegra_xudc_process_event_ring(")
        self.assertLess(consume.index("READ_ONCE(event->control)"), consume.index("dma_rmb();"))
        self.assertLess(consume.index("dma_rmb();"),
                        consume.index("tegra_xudc_handle_event("))

    def test_iso_snapshot_once_and_before_halt(self):
        source = (ROOT / "tegra-xudc.c").read_text()
        snapshot = extract_function(source, "dump_iso_ring_state(")
        worker = extract_function(source, "tegra_xudc_iso_snapshot_work(")
        self.assertNotIn("dev_err(", snapshot)
        self.assertIn("memcpy(&snapshot->context", snapshot)
        self.assertIn("memcpy(snapshot->ring", snapshot)
        self.assertIn("snapshot->event = *event", snapshot)
        self.assertLess(snapshot.index("ep_halt(xudc, ep->index);"),
                        snapshot.index("schedule_work(&xudc->iso_snapshot_work);"))
        self.assertIn("snapshot->ring", worker)
        self.assertIn("snapshot->context", worker)
        self.assertIn("snapshot->event", worker)
        self.assertIn("snapshot->regs", worker)
        self.assertIn("INIT_WORK(&xudc->iso_snapshot_work, tegra_xudc_iso_snapshot_work);", source)
        self.assertIn("cancel_work_sync(&xudc->iso_snapshot_work);", source)
        event = extract_function(source, "tegra_xudc_handle_transfer_event(")
        self.assertIn("dump_iso_ring_state(ep, event);", event)


if __name__ == "__main__":
    unittest.main()
