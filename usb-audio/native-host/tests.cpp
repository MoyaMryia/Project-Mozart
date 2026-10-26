#include "runtime.hpp"
#include "wav.hpp"
#include "session.hpp"
#include <cstring>
#include <iostream>

using namespace mozart_host;
namespace {
unsigned checks = 0;
void expect(bool ok, const std::string& message) { ++checks; require(ok, "test failed: " + message); }
template<class F> void rejects(F f, const std::string& fragment) {
    try { f(); }
    catch (const std::runtime_error& e) { expect(std::string(e.what()).find(fragment) != std::string::npos, e.what()); return; }
    throw std::runtime_error("expected rejection: " + fragment);
}
void codec() {
    auto f = hello(); f.seq = 0x12345678; f.stamp = 0x0102030405060708ULL;
    const std::vector<uint8_t> golden{
        0x5a,0x4d,1,1,1,0,0,0,0x78,0x56,0x34,0x12,8,7,6,5,4,3,2,1,9,0,0,0,
        1,0,1,9,0,0,0,64,0};
    auto bytes = encode(f); expect(bytes == golden, "HELLO wire golden");
    for (size_t split = 0; split <= bytes.size(); ++split) {
        Decoder decoder; auto a = decoder.push(bytes.data(), split);
        auto b = decoder.push(bytes.data() + split, bytes.size() - split);
        expect(a.size() + b.size() == 1, "all split positions decode one frame"); decoder.finish();
        const auto& frame = a.empty() ? b[0] : a[0];
        expect(frame.seq == f.seq && frame.stamp == f.stamp && frame.body == f.body, "split payload preserved");
    }
    Decoder bytewise; unsigned count = 0;
    for (auto b : bytes) count += unsigned(bytewise.push(&b, 1).size());
    expect(count == 1, "bytewise decoder"); bytewise.finish();
    Frame future; future.type = 99; future.stream = 200; future.body.resize(70000, 0xaa);
    auto joined = bytes; auto unknown = encode(future);
    joined.insert(joined.end(), unknown.begin(), unknown.end()); joined.insert(joined.end(), bytes.begin(), bytes.end());
    Decoder merged; auto frames = merged.push(joined.data(), joined.size());
    expect(frames.size() == 3 && frames[1].body.size() == 70000 && !audio(frames[1]) && !control(frames[1]), "coalesced future stream skipped by length");
    expect(frames[2].seq == f.seq, "frame following future stream"); merged.finish();
    Frame empty; auto no_body = encode(empty); Decoder zero;
    expect(zero.push(no_body.data(), no_body.size()).size() == 1, "zero length frame");
    for (auto offset : {MOZART_USB_MAGIC_OFFSET, MOZART_USB_VERSION_OFFSET, MOZART_USB_RESERVED_OFFSET, MOZART_USB_RESERVED_OFFSET + 1}) {
        auto bad = bytes; bad[size_t(offset)] ^= 0x80;
        rejects([&] { Decoder d; d.push(bad.data(), bad.size()); }, "USB");
    }
    auto bad = bytes; bad[MOZART_USB_FLAGS_OFFSET] = 0x80;
    rejects([&] { Decoder d; d.push(bad.data(), bad.size()); }, "flags");
    bad = bytes; put32(bad.data() + MOZART_USB_LENGTH_OFFSET, MOZART_USB_MAX_PAYLOAD + 1);
    rejects([&] { Decoder d; d.push(bad.data(), MOZART_USB_HEADER_BYTES); }, "limit");
    bad = bytes; put32(bad.data() + MOZART_USB_LENGTH_OFFSET, UINT32_MAX);
    rejects([&] { Decoder d; d.push(bad.data(), bad.size()); }, "limit");
    Decoder partial; partial.push(bytes.data(), bytes.size() - 1);
    rejects([&] { partial.finish(); }, "truncated");
    Frame maximum; maximum.body.resize(MOZART_USB_MAX_PAYLOAD);
    auto large = encode(maximum); Decoder bounded;
    for (size_t i = 0; i < large.size(); i += 997) {
        auto result = bounded.push(large.data() + i, std::min<size_t>(997, large.size() - i));
        if (!result.empty()) expect(result[0].body.size() == MOZART_USB_MAX_PAYLOAD, "maximum legal payload");
    }
    bounded.finish();
}
void negotiation() {
    auto ack = hello(); put16(ack.body.data(), MOZART_USB_CONTROL_HELLO_ACK);
    expect(hello_ack(ack) == MOZART_USB_FAST64_SAMPLES, "selected64 proposal");
    put16(ack.body.data() + 7, MOZART_USB_FAST_SAMPLES);
    expect(hello_ack(ack) == MOZART_USB_FAST_SAMPLES, "selected128 proposal");
    auto bad = ack; bad.body.push_back(0); rejects([&] { hello_ack(bad); }, "shape");
    bad = ack; bad.body[2] = 2; rejects([&] { hello_ack(bad); }, "version");
    bad = ack; put16(bad.body.data() + 7, 32); rejects([&] { hello_ack(bad); }, "block");
    bad = ack; put16(bad.body.data() + 7, MOZART_USB_FAST64_SAMPLES); put32(bad.body.data() + 3, 0);
    rejects([&] { hello_ack(bad); }, "capability");
    auto start = command(MOZART_USB_CONTROL_START_ACK, MOZART_USB_START_BYTES);
    for (unsigned block : {MOZART_USB_FAST64_SAMPLES, MOZART_USB_FAST_SAMPLES}) {
        put16(start.body.data(), MOZART_USB_CONTROL_START_ACK);
        put16(start.body.data() + 2, uint16_t(block)); start_ack(start, block);
        const unsigned other = block == MOZART_USB_FAST64_SAMPLES ? MOZART_USB_FAST_SAMPLES : MOZART_USB_FAST64_SAMPLES;
        rejects([&] { start_ack(start, other); }, "mismatch");
        start.flags = MOZART_USB_FLAG_START; rejects([&] { start_ack(start, block); }, "shape"); start.flags = 0;
        auto short_ack = start; short_ack.body.pop_back(); rejects([&] { start_ack(short_ack, block); }, "shape");
        put16(start.body.data(), MOZART_USB_CONTROL_HELLO_ACK); rejects([&] { start_ack(start, block); }, "mismatch");
    }
    auto stop = command(MOZART_USB_CONTROL_STOP_ACK, MOZART_USB_STOP_ACK_BYTES);
    put32(stop.body.data() + 2, 123); put32(stop.body.data() + 6, 123); stop_ack(stop, 123, 123);
    rejects([&] { stop_ack(stop, 123, 122); }, "coverage");
    put32(stop.body.data() + 6, 122); rejects([&] { stop_ack(stop, 123, 123); }, "coverage");
    stop.body.pop_back(); rejects([&] { stop_ack(stop, 123, 123); }, "shape");
}
struct ScriptedReads {
    struct Read { std::vector<uint8_t> bytes; };
    std::deque<std::vector<uint8_t>> chunks;
    unsigned delay_ms = 0;
    bool disconnected = false;
    Read read(unsigned timeout_ms) {
        if (disconnected) throw std::runtime_error("USB receive: LIBUSB_ERROR_NO_DEVICE");
        if (delay_ms) std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
        if (chunks.empty()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(std::min(timeout_ms, 1u))); return {};
        }
        Read r{std::move(chunks.front())}; chunks.pop_front(); return r;
    }
};
struct MockEndpoint : ScriptedReads {
    unsigned proposal = MOZART_USB_FAST64_SAMPLES, block = MOZART_USB_FAST_SAMPLES, reply_block = 0;
    bool hello_supported = true, start_supported = true, stop_supported = true, stopped = false;
    uint32_t rx = 0, tx = 0;
    std::vector<unsigned> starts;
    unsigned hellos = 0;
    void send(const Frame& f) {
        if (audio(f)) {
            require(!stopped, "mock audio after STOP");
            require(f.seq == rx && f.body.size() == block * FrameBytes, "mock audio session mismatch");
            require(f.stream == (block == MOZART_USB_FAST64_SAMPLES ? MOZART_USB_STREAM_FAST64 : MOZART_USB_STREAM_FAST),
                    "mock audio block mismatch");
            ++rx; ++tx; chunks.push_back(encode(f)); return;
        }
        require(control(f), "mock expected control");
        switch (u16(f.body.data())) {
        case MOZART_USB_CONTROL_HELLO: {
            ++hellos;
            if (!hello_supported) return;
            auto ack = hello(); put16(ack.body.data(), MOZART_USB_CONTROL_HELLO_ACK);
            put16(ack.body.data() + 7, uint16_t(proposal)); chunks.push_back(encode(ack)); break;
        }
        case MOZART_USB_CONTROL_START: {
            starts.push_back(u16(f.body.data() + 2));
            if (!start_supported) return;
            block = starts.back(); rx = tx = 0; stopped = false;
            auto ack = command(MOZART_USB_CONTROL_START_ACK, MOZART_USB_START_BYTES);
            put16(ack.body.data() + 2, uint16_t(reply_block ? reply_block : block)); chunks.push_back(encode(ack)); break;
        }
        case MOZART_USB_CONTROL_STOP: {
            stopped = true;
            if (!stop_supported) return;
            auto ack = command(MOZART_USB_CONTROL_STOP_ACK, MOZART_USB_STOP_ACK_BYTES);
            put32(ack.body.data() + 2, rx); put32(ack.body.data() + 6, tx); chunks.push_back(encode(ack)); break;
        }
        default: throw std::runtime_error("mock unexpected opcode");
        }
    }
};
void session_negotiation() {
    auto transfer = [](MockEndpoint& peer, auto& receiver, unsigned block, unsigned count) {
        Frame response;
        for (unsigned seq = 0; seq < count; ++seq) {
            Frame f; f.stream = block == MOZART_USB_FAST64_SAMPLES ? MOZART_USB_STREAM_FAST64 : MOZART_USB_STREAM_FAST;
            f.seq = seq; f.body.resize(block * FrameBytes); peer.send(f);
            expect(receiver.next(response, 10) && response.seq == seq && response.body == f.body, "mock audio exchange");
        }
        peer.send(command(MOZART_USB_CONTROL_STOP, 2));
        expect(receiver.next(response, 10), "mock STOP_ACK required"); stop_ack(response, count, count); receiver.finish();
        Frame rejected; rejected.body.resize(block * FrameBytes);
        rejects([&] { peer.send(rejected); }, "after STOP");
    };
    MockEndpoint peer; Receiver receiver(peer);
    const unsigned blocks[] = {128, 128, 64, 128};
    for (unsigned session = 0; session < 4; ++session) {
        const bool legacy = blocks[session] == MOZART_USB_FAST_SAMPLES;
        auto selection = negotiate_session(legacy, peer, receiver);
        expect(selection.block == blocks[session] && selection.negotiated == !legacy, "START selects session block");
        expect(selection.fallback_reason == (legacy ? "legacy_requested" : ""), "new peer acknowledges START128 without timeout fallback");
        transfer(peer, receiver, blocks[session], session + 1);
    }
    expect(peer.starts == std::vector<unsigned>({128, 128, 64, 128}) && peer.hellos == 1, "legacy sends START128 without HELLO");
    MockEndpoint selected128; selected128.proposal = MOZART_USB_FAST_SAMPLES; Receiver selected(selected128);
    for (unsigned session = 0; session < 2; ++session) {
        auto selection = negotiate_session(false, selected128, selected);
        expect(selection.block == MOZART_USB_FAST_SAMPLES && !selection.negotiated &&
               selection.fallback_reason == "hello_selected_legacy128", "HELLO128 commits START_ACK128");
        transfer(selected128, selected, selection.block, session + 1);
    }
    for (bool legacy : {false, true}) {
        MockEndpoint old; old.hello_supported = false; old.start_supported = false; Receiver old_receiver(old);
        auto selection = negotiate_session(legacy, old, old_receiver);
        expect(selection.block == MOZART_USB_FAST_SAMPLES && !selection.negotiated && old.starts == std::vector<unsigned>({128}),
               "old peer START128 timeout retains first-session raw128");
        expect(selection.fallback_reason == std::string(legacy ? "legacy_requested" : "hello_timeout_legacy128") +
               ";start_ack_timeout_legacy128", "old peer START_ACK absence has queryable reason");
        transfer(old, old_receiver, selection.block, 2);
    }
    MockEndpoint no_hello; no_hello.hello_supported = false; Receiver raw_new(no_hello);
    auto selection = negotiate_session(false, no_hello, raw_new);
    expect(selection.fallback_reason == "hello_timeout_legacy128" && no_hello.starts == std::vector<unsigned>({128}),
           "HELLO timeout still validates new peer START_ACK128");
    MockEndpoint missing64; missing64.start_supported = false; Receiver mandatory(missing64);
    rejects([&] { negotiate_session(false, missing64, mandatory); }, "START_ACK timeout");
    for (bool legacy : {false, true}) {
        MockEndpoint bad; bad.reply_block = legacy ? MOZART_USB_FAST64_SAMPLES : MOZART_USB_FAST_SAMPLES; Receiver mismatch(bad);
        rejects([&] { negotiate_session(legacy, bad, mismatch); }, "START_ACK mismatch");
    }
    MockEndpoint no_stop; no_stop.start_supported = false; no_stop.stop_supported = false; Receiver no_stop_receiver(no_stop);
    selection = negotiate_session(true, no_stop, no_stop_receiver);
    no_stop.send(command(MOZART_USB_CONTROL_STOP, 2)); Frame response;
    expect(!no_stop_receiver.next(response, 2), "old peer without STOP_ACK does not manufacture coverage");
    rejects([&] { stop_deadline(100, 100); }, "STOP_ACK timeout");
}
void reception() {
    auto ack = hello(); put16(ack.body.data(), MOZART_USB_CONTROL_HELLO_ACK); auto b = encode(ack);
    ScriptedReads split; split.chunks = {{b.begin(), b.begin() + 7}, {b.begin() + 7, b.end()}};
    Receiver fragmented(split); Frame received;
    expect(fragmented.handshake(received, 500, true, hello_ack) && hello_ack(received) == MOZART_USB_FAST64_SAMPLES, "fragmented handshake uses shared decoder");
    ScriptedReads none; Receiver absent(none);
    expect(!absent.handshake(received, 2, true, hello_ack), "HELLO deadline permits timeout fallback");
    ScriptedReads late; late.chunks.push_back(b); late.delay_ms = 10; Receiver late_hello(late);
    expect(!late_hello.handshake(received, 1, true, hello_ack), "late HELLO_ACK cannot bypass deadline");
    auto malformed = ack; malformed.body.pop_back();
    ScriptedReads invalid; invalid.chunks.push_back(encode(malformed)); invalid.delay_ms = 10; Receiver bad_late(invalid);
    rejects([&] { bad_late.handshake(received, 1, true, hello_ack); }, "shape");
    auto start = command(MOZART_USB_CONTROL_START_ACK, MOZART_USB_START_BYTES);
    put16(start.body.data() + 2, MOZART_USB_FAST64_SAMPLES);
    ScriptedReads commit; commit.chunks.push_back(encode(start)); commit.delay_ms = 10; Receiver late_commit(commit);
    rejects([&] { late_commit.handshake(received, 1, false, [&](const Frame& f) { start_ack(f, MOZART_USB_FAST64_SAMPLES); }); }, "START_ACK timeout");
    ScriptedReads partial; partial.chunks.push_back({b.begin(), b.begin() + 10}); Receiver truncated(partial);
    rejects([&] { truncated.handshake(received, 2, true, hello_ack); }, "truncated");
    ScriptedReads unplugged; unplugged.disconnected = true; Receiver failure(unplugged);
    rejects([&] { failure.handshake(received, 500, true, hello_ack); }, "NO_DEVICE");
    Frame future; future.type = 200; future.stream = 201; future.body = {1, 2, 3};
    auto coalesced = encode(future); coalesced.insert(coalesced.end(), b.begin(), b.end());
    ScriptedReads unknown; unknown.chunks.push_back(coalesced); Receiver skipping(unknown);
    expect(skipping.handshake(received, 500, true, hello_ack) && hello_ack(received) == MOZART_USB_FAST64_SAMPLES, "future frame before HELLO_ACK");
    Frame pcm; pcm.body.resize(MOZART_USB_FAST_BYTES);
    ScriptedReads premature; premature.chunks.push_back(encode(pcm)); Receiver early(premature);
    rejects([&] { early.handshake(received, 500, true, hello_ack); }, "before negotiation");
    auto stop = command(MOZART_USB_CONTROL_STOP_ACK, MOZART_USB_STOP_ACK_BYTES);
    coalesced = encode(stop); auto trailing = encode(future);
    coalesced.insert(coalesced.end(), trailing.begin(), trailing.end());
    ScriptedReads stopped; stopped.chunks.push_back(coalesced); Receiver clean(stopped);
    expect(clean.next(received, 1), "STOP_ACK in coalesced read"); clean.finish();
    coalesced = encode(stop); trailing = encode(pcm); coalesced.insert(coalesced.end(), trailing.begin(), trailing.end());
    ScriptedReads trailing_audio; trailing_audio.chunks.push_back(coalesced); Receiver extra(trailing_audio);
    expect(extra.next(received, 1), "STOP before trailing audio");
    rejects([&] { extra.finish(); }, "after STOP_ACK");
}
void association() {
    Ledger ledger; Frame f; f.seq = 7; f.stamp = 100;
    ledger.submit({7, 100, 63});
    auto bad = f; bad.seq = 8; rejects([&] { ledger.receive(bad, true, 200); }, "sequence");
    bad = f; bad.stamp = 101; rejects([&] { ledger.receive(bad, true, 200); }, "timestamp");
    rejects([&] { ledger.receive(f, true, 99); }, "clock");
    rejects([&] { ledger.receive(f, true, 100 + uint64_t(MOZART_USB_IO_TIMEOUT_MS) * 1000 + 1); }, "deadline");
    auto p = ledger.receive(f, true, 200); expect(p.valid == 63 && p.sent == 100, "valid response preserves source tail length");
    rejects([&] { ledger.receive(f, true, 200); }, "sequence");
    ledger.submit({7, 100, 64}); f.stamp = UINT64_MAX;
    expect(ledger.receive(f, false, 200).sent == 100, "legacy ignores remote clock and uses host association");
    ledger.check_deadline();
    ledger.submit({8, now_us() - uint64_t(MOZART_USB_IO_TIMEOUT_MS) * 1000 - 1, 64});
    rejects([&] { ledger.check_deadline(); }, "deadline");
    Ledger full;
    for (size_t i = 0; i < PendingLimit; ++i) full.submit({uint32_t(i), now_us(), 64});
    rejects([&] { full.submit({uint32_t(PendingLimit), now_us(), 64}); }, "full");
    stop_deadline(100, 0); stop_deadline(99, 100);
    rejects([&] { stop_deadline(100, 100); }, "STOP_ACK timeout");
    rejects([&] { stop_deadline(101, 100); }, "STOP_ACK timeout");
}
void queue_estimates() {
    CaptureLedger<Ledger> ledger;
    Frame f; f.seq = Warmup; f.stamp = 1000;
    ledger.submit({f.seq, f.stamp, 64, 48});
    auto bad = f; ++bad.seq;
    rejects([&] { ledger.receive(bad, true, 1500); }, "sequence");
    bad = f; ++bad.stamp;
    rejects([&] { ledger.receive(bad, true, 1500); }, "timestamp");
    auto p = ledger.receive(f, true, 1500);
    expect(p.capture_delay_frames == 48 && p.sent == 1000 && p.valid == 64, "capture metadata survives rejected responses");
    Status s;
    s.record_alsa_queue_estimate(p.seq, p.sent, *p.capture_delay_frames, 2000, 192);
    expect(s.alsa_e2e_queue_estimate_us.count == 1 && s.alsa_e2e_queue_estimate_us.maximum == 6000,
           "oldest sample formula: 1000 us plus 240 frames equals 6000 us");
    expect(s.alsa_e2e_over_budget == 0, "normal estimate fits budget");
    s.record_alsa_queue_estimate(101, 1000, 48, 6000, 192);
    expect(s.alsa_e2e_queue_estimate_us.maximum == 10000 && s.alsa_e2e_over_budget == 0, "budget equality passes");
    s.record_alsa_queue_estimate(102, 1000, 48, 6001, 192);
    expect(s.alsa_e2e_queue_estimate_us.maximum == 10001 && s.alsa_e2e_over_budget == 1, "one microsecond over budget is counted");
    s.record_alsa_queue_estimate(103, 1000, 0, 12000, 0);
    expect(s.alsa_e2e_over_budget == 2 && s.alsa_e2e_queue_estimate_us.maximum == 11000, "artificial software wait exceeds budget");
    s.record_alsa_queue_estimate(104, 1000, 96, 2000, 480);
    expect(s.alsa_e2e_over_budget == 3 && s.alsa_e2e_queue_estimate_us.maximum == 13000, "artificial ALSA queues exceed budget");
    expect(s.alsa_e2e_max_seq == 104 && s.alsa_e2e_max_capture_frames == 96 &&
           s.alsa_e2e_max_playback_frames == 480 && s.alsa_e2e_max_delta_us == 1000, "maximum estimate retains packet evidence");
    expect(s.state == "warming" && s.reason.empty(), "budget excess does not terminate stream");
    s.record_alsa_queue_estimate(105, 1000, 1, 2000, 1);
    expect(s.alsa_e2e_queue_estimate_us.count == 6 && s.alsa_e2e_max_seq == 104, "lower estimate preserves maximum evidence");
    Status fractional;
    fractional.record_alsa_queue_estimate(100, 1000, 1, 2000, 1);
    expect(fractional.alsa_e2e_queue_estimate_us.maximum == 1041, "fractional frame duration truncates once after summation");
    ++f.seq; f.stamp = 2000;
    ledger.submit({f.seq, f.stamp, 63, std::nullopt});
    auto file = ledger.receive(f, true, 2500);
    expect(!file.capture_delay_frames && file.sent == 2000 && file.valid == 63, "file packets have no capture estimate");
    expect(Status{}.alsa_e2e_queue_estimate_us.count == 0, "file status starts with zero complete chain estimates");
}
void packet_replay() {
    for (bool file_mode : {false, true}) {
        CaptureLedger<Ledger> ledger; Decoder decoder; Status s;
        struct Item { unsigned seq; uint64_t sent; std::optional<int64_t> capture; };
        Queue<Item, 128> queue; std::vector<Frame> packets;
        for (unsigned seq = 0; seq < 113; ++seq) {
            Frame tx; tx.seq = seq; tx.stamp = 1000 + seq * 2000;
            tx.body.resize(64 * FrameBytes, uint8_t(seq));
            ledger.submit({seq, tx.stamp, seq == 112 ? 13u : 64u,
                           file_mode ? std::nullopt : std::optional<int64_t>(seq == 110 ? 384 : (seq % 4) * 16)});
            packets.push_back(std::move(tx));
        }
        for (const auto& tx : packets) {
            auto seq = tx.seq;
            auto wire = encode(tx); auto responses = decoder.push(wire.data(), wire.size());
            expect(responses.size() == 1 && responses[0].body == tx.body, "packet replay preserves wire payload");
            auto p = ledger.receive(responses[0], true, tx.stamp + 500);
            expect(p.seq == seq && p.sent == tx.stamp && p.valid == (seq == 112 ? 13u : 64u), "inflight packet sequence, sent time and source tail stay associated");
            expect(file_mode ? !p.capture_delay_frames : p.capture_delay_frames == (seq == 110 ? 384 : (seq % 4) * 16),
                   "distinct inflight capture delays stay associated");
            ++s.rx;
            if (seq < Warmup) ++s.warmup;
            if (file_mode || seq >= Warmup) queue.put({p.seq, p.sent, p.capture_delay_frames});
        }
        decoder.finish(); queue.close(); Failure failure; Item item;
        uint64_t written = 0;
        while (queue.take(item, failure, 0)) {
            ++written;
            if (item.seq >= Warmup) ++s.played;
            if (!file_mode) s.record_alsa_queue_estimate(item.seq, item.sent, *item.capture, item.sent + 1000, 192);
        }
        expect(s.rx == 113 && s.warmup == 100 && s.played == 13, "replay excludes exactly 100 warmup packets");
        if (file_mode) {
            expect(written == 113 && s.alsa_e2e_queue_estimate_us.count == 0 && s.alsa_e2e_over_budget == 0,
                   "file replay retains warmup audio without complete ALSA estimates");
        } else {
            expect(written == 13 && s.alsa_e2e_queue_estimate_us.count == 13 && s.alsa_e2e_over_budget == 1,
                   "ALSA replay estimates each played packet and detects queue excess");
            expect(s.alsa_e2e_queue_estimate_us.maximum == 13000 && s.alsa_e2e_max_seq == 110 &&
                   s.alsa_e2e_max_capture_frames == 384 && s.alsa_e2e_max_playback_frames == 192 && s.alsa_e2e_max_delta_us == 1000,
                   "replay maximum evidence follows its capture packet through ledger and playback queue");
        }
    }
}
void trace_failure() {
    auto dir = std::filesystem::path(HOST_TEST_DIR) / "pcm-failed-trace";
    std::filesystem::create_directories(dir);
    struct Record { bool sent; unsigned seq, valid; };
    Queue<Record, 8192> queue; Failure failure; Status cleanup_status;
    for (unsigned i = 0; i < 2400; ++i) queue.put({i % 2 == 0, i, i == 2300 ? 1u : 64u});
    uint64_t input_frames = 0, output_frames = 0; unsigned consumed = 0, flushes = 0;
    {
        WavWriter input((dir / "capture.wav").string()), output((dir / "response.wav").string());
        RegularFile csv((dir / "packets.csv").string(), true);
        std::string buffer = "direction,seq,host_us,wire_timestamp_us,rtt_us,source_frames\n";
        std::vector<uint8_t> bytes(64 * FrameBytes, 0x5a);
        rejects([&] {
            consume_trace(queue, failure, [&](const Record& t) {
                (t.sent ? input : output).write(bytes.data(), t.valid);
                (t.sent ? input_frames : output_frames) += t.valid;
                buffer += std::string(t.sent ? "tx," : "rx,") + std::to_string(t.seq) + ",123456789,123456789,1000," + std::to_string(t.valid) + '\n';
                if (buffer.size() >= 65536) { csv.write(buffer); buffer.clear(); ++flushes; }
                if (++consumed == 2301) {
                    failure.record(std::make_exception_ptr(std::runtime_error("PCM playback: Broken pipe")));
                }
            }, [&] {
                expect(!buffer.empty() && buffer.size() < 65536, "failure retains unflushed CSV tail");
                finish_trace([&](const std::exception& e) {
                    cleanup_status.trace_cleanup_reason = e.what(); failure.record();
                }, [&] { csv.write(buffer); }, [&] { input.finish(); }, [&] { output.finish(); }, [&] { csv.close(); });
            });
        }, "PCM playback: Broken pipe");
    }
    expect(consumed == 2301 && flushes > 0 && queue.sizes().first == 99, "failure saves completed records without consuming pending packets");
    expect(cleanup_status.trace_cleanup_reason.empty(), "successful PCM trace cleanup has no cleanup error");
    std::ifstream csv(dir / "packets.csv"); std::string row;
    std::getline(csv, row); uint64_t tx_frames = 0, rx_frames = 0; unsigned rows = 0;
    while (std::getline(csv, row)) {
        auto frames = integer(row.substr(row.rfind(',') + 1));
        (row.rfind("tx,", 0) == 0 ? tx_frames : rx_frames) += frames; ++rows;
    }
    expect(rows == 2301 && tx_frames == input_frames && rx_frames == output_frames, "all completed CSV records survive PCM failure");
    WavReader input((dir / "capture.wav").string()), output((dir / "response.wav").string());
    expect(input.frames == tx_frames && output.frames == rx_frames, "failure WAV headers match CSV frame totals");
    expect(std::filesystem::file_size(dir / "capture.wav") == 44 + tx_frames * FrameBytes &&
           std::filesystem::file_size(dir / "response.wav") == 44 + rx_frames * FrameBytes, "failure WAV bodies match CSV frame totals");
    rejects([&] { failure.check(); }, "PCM playback: Broken pipe");
    {
        Queue<unsigned> failed_queue; Failure original; Status failed_status;
        WavWriter input((dir / "double-failure-capture.wav").string()), output((dir / "double-failure-response.wav").string());
        RegularFile csv((dir / "double-failure.csv").string(), true);
        std::vector<uint8_t> bytes(64 * FrameBytes, 0x5a);
        input.write(bytes.data(), 64); output.write(bytes.data(), 64);
        original.record(std::make_exception_ptr(std::runtime_error("PCM playback: Broken pipe")));
        bool closed = false;
        rejects([&] {
            consume_trace(failed_queue, original, [](unsigned) {}, [&] {
                finish_trace([&](const std::exception& e) {
                    if (failed_status.trace_cleanup_reason.empty()) failed_status.trace_cleanup_reason = e.what();
                    original.record();
                }, [] { throw std::runtime_error("CSV tail failure"); }, [&] { input.finish(); }, [&] { output.finish(); },
                   [&] { csv.close(); closed = true; });
            });
        }, "PCM playback: Broken pipe");
        expect(closed && failed_status.trace_cleanup_reason == "CSV tail failure", "cleanup error is queryable and does not skip later cleanup");
        WavReader capture((dir / "double-failure-capture.wav").string()), response((dir / "double-failure-response.wav").string());
        expect(capture.frames == 64 && response.frames == 64, "CSV cleanup failure preserves both completed WAV bodies and headers");
        rejects([&] { original.check(); }, "PCM playback: Broken pipe");
    }
    {
        Queue<unsigned> records; Failure consume_failure; records.put(64);
        WavWriter wav((dir / "consume-failure.wav").string());
        std::vector<uint8_t> bytes(64 * FrameBytes, 0x5a); bool finalized = false;
        rejects([&] {
            consume_trace(records, consume_failure, [&](unsigned frames) {
                wav.write(bytes.data(), frames);
                throw std::runtime_error("CSV buffer write failure");
            }, [&] {
                finish_trace([&](const std::exception&) { consume_failure.record(); }, [&] { wav.finish(); finalized = true; });
            });
        }, "CSV buffer write failure");
        WavReader saved((dir / "consume-failure.wav").string());
        expect(finalized && saved.frames == 64, "consume callback failure also finalizes completed WAV data");
    }
    Failure cleanup_only;
    finish_trace([&](const std::exception&) { cleanup_only.record(); }, [] { throw std::runtime_error("CSV close failure"); });
    rejects([&] { cleanup_only.check(); }, "CSV close failure");
    Queue<unsigned> waiting; Failure setup_failure; std::thread trace; bool finished = false;
    {
        ThreadJoiner joiner{setup_failure, {&trace}};
        trace = std::thread([&] {
            try { consume_trace(waiting, setup_failure, [](unsigned) {}, [&] { finished = true; }); }
            catch (const std::runtime_error& e) { expect(std::string(e.what()) == "worker setup interrupted", "trace setup cancellation preserves failure"); }
        });
    }
    expect(finished && !trace.joinable(), "trace cleanup permits partial thread creation cancellation");
}
void queues() {
    Queue<unsigned> queue; Failure failure;
    for (unsigned i = 0; i < QueueLimit; ++i) queue.put(i);
    rejects([&] { queue.put(99); }, "full");
    expect(queue.sizes() == std::make_pair(QueueLimit, QueueLimit), "full queue remains unchanged");
    queue.close(); rejects([&] { queue.put(99); }, "closed");
    for (unsigned i = 0; i < QueueLimit; ++i) {
        unsigned value = 99; expect(queue.take(value, failure) && value == i, "closed queue drains in order");
    }
    unsigned value = 99; expect(!queue.take(value, failure) && queue.done(), "queue EOF after drain");
    Queue<unsigned> waiting; std::thread worker;
    Failure interrupted;
    {
        ThreadJoiner joiner{interrupted, {&worker}};
        worker = std::thread([&] {
            try { unsigned v; while (!waiting.take(v, interrupted, 1)) {} }
            catch (const std::runtime_error& e) { expect(std::string(e.what()) == "worker setup interrupted", "joiner cancellation"); }
        });
    }
    expect(!worker.joinable() && interrupted.failed.load(), "exception-safe thread join");
    Failure original; std::thread canceled;
    try {
        ThreadJoiner joiner{original, {&canceled}};
        try {
            canceled = std::thread([&] { while (!original.failed.load()) std::this_thread::yield(); });
            throw std::runtime_error("thread resource error");
        } catch (const std::runtime_error&) { original.record(); throw; }
    } catch (const std::runtime_error&) {}
    rejects([&] { original.check(); }, "thread resource error");
}
void status() {
    Status s; expect(s.state == "warming", "initial state"); s.ready(); expect(s.state == "ready", "ready transition");
    s.stop(); expect(s.state == "stopped", "stop transition"); s.ready(); expect(s.state == "stopped", "terminal stopped state");
    s.fail("first\n\"\\错误"); auto reason = s.reason;
    s.fail("second"); s.stop(); s.ready(); expect(s.state == "failed" && s.reason == reason, "failure reason survives all transitions");
    Status short_session; short_session.stop(); expect(short_session.state == "stopped", "finite session shorter than warmup");
    expect(json_string("\"\\\n\r\t\b\f") == "\"\\\"\\\\\\u000a\\u000d\\u0009\\u0008\\u000c\"", "JSON control escaping");
    expect(json_string("错误😀") == "\"错误😀\"", "UTF-8 preserved");
    expect(json_string(std::string(1, char(0xff))) == "\"\\u00ff\"", "invalid UTF-8 encoded safely");
    Histogram h; for (unsigned i = 0; i < 100; ++i) h.add(i * 32);
    expect(h.percentile(50) == 49 * 32 + 31 && h.percentile(99) == 98 * 32 + 31, "bounded histogram percentiles");
    h.add(1000000); expect(h.maximum == 1000000, "histogram exact maximum");
    Histogram single; single.add(3); expect(single.percentile(50) == 3, "quantile does not exceed maximum");
    expect(Histogram{}.percentile(99) == 0, "empty histogram");
    auto path = std::filesystem::path(HOST_TEST_DIR) / "status.json";
    save_status(path.string(), s.json());
    std::ifstream in(path); std::string content((std::istreambuf_iterator<char>(in)), {});
    expect(content == s.json() && !std::filesystem::exists(path.string() + ".tmp"), "atomic status replacement");
    s.rx = 77; save_status(path.string(), s.json());
    std::ifstream again(path); content.assign(std::istreambuf_iterator<char>(again), {});
    expect(content == s.json(), "status overwrites previous file");
}
void cli() {
    auto o = parse({}); expect(o.capture == "default" && o.playback == "default" && o.periods == 6 && o.prefill == 4 && o.target == 3, "production defaults");
    expect(o.status == "build/run/usb-native.json", "status default");
    expect(parse({"--seconds", "60001"}).seconds == 60001, "seconds has no artificial duration cap");
    expect(parse({"--seconds", "0"}).seconds == 0, "zero seconds means no sample limit");
    expect(parse({"--capture", "explicit"}).playback == "default", "individual explicit device");
    expect(parse({"--input", "input.wav", "--output", "output.wav", "--pace", "--legacy"}).pace, "paced file mode");
    for (auto args : std::vector<std::vector<std::string>>{
        {"--unknown"}, {"--seconds"}, {"--seconds", ""}, {"--seconds", "-1"}, {"--seconds", "+1"}, {"--seconds", "2x"},
        {"--seconds", "18446744073709551616"}, {"--seconds", "18446744073709551615"},
        {"--periods", "0"}, {"--periods", "4294967296"}, {"--prefill", "6"}, {"--target", "1"},
        {"--input", "x"}, {"--output", "x"}, {"--pace"}, {"--capture", ""},
        {"--input", "x", "--output", "x"}, {"--input", "x", "--output", "y", "--playback", "default"},
        {"--input", "x", "--output", "y", "--status-file", "x"},
        {"--input", "x.tmp", "--output", "y", "--status-file", "x"},
        {"--input", "trace/capture.wav", "--output", "y", "--trace-dir", "trace"},
        {"--input", "x", "--output", "trace/response.wav", "--trace-dir", "trace"}}) {
        bool rejected = false; try { parse(args); } catch (const std::runtime_error&) { rejected = true; }
        expect(rejected, "invalid CLI rejected");
    }
}
void wav() {
    std::filesystem::create_directories(HOST_TEST_DIR);
    auto path = (std::filesystem::path(HOST_TEST_DIR) / "test.wav").string();
    std::vector<uint8_t> samples(65 * FrameBytes);
    for (size_t i = 0; i < samples.size(); ++i) samples[i] = uint8_t(i);
    { WavWriter out(path); out.write(samples.data(), 65); out.finish(); }
    WavReader in(path); expect(in.frames == 65, "WAV source length");
    std::vector<uint8_t> block(64 * FrameBytes, 255);
    expect(in.read(block, 64) == 64 && std::equal(block.begin(), block.end(), samples.begin()), "WAV data excludes header");
    expect(in.read(block, 64) == 1, "nonaligned tail length");
    expect(std::equal(block.begin(), block.begin() + FrameBytes, samples.begin() + 64 * FrameBytes) &&
           std::all_of(block.begin() + FrameBytes, block.end(), [](uint8_t b) { return b == 0; }), "tail zero padding");
    expect(in.read(block, 64) == 0, "WAV finite EOF");
    auto bad = (std::filesystem::path(HOST_TEST_DIR) / "bad.wav").string();
    { std::ofstream out(bad, std::ios::binary); out.write(reinterpret_cast<const char*>(samples.data()), samples.size()); }
    rejects([&] { WavReader invalid(bad); }, "RIFF");
    std::ifstream source(path, std::ios::binary);
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(source)), {});
    for (auto offset : {20, 22, 24, 28, 32, 34, 40}) {
        auto b = bytes; b[size_t(offset)] ^= 1;
        std::ofstream out(bad, std::ios::binary | std::ios::trunc); out.write(reinterpret_cast<const char*>(b.data()), b.size()); out.close();
        rejects([&] { WavReader invalid(bad); }, "WAV");
    }
    auto b = bytes; b.resize(b.size() - 1);
    { std::ofstream out(bad, std::ios::binary | std::ios::trunc); out.write(reinterpret_cast<const char*>(b.data()), b.size()); }
    rejects([&] { WavReader invalid(bad); }, "length");
    // 验证奇数字节扩展块以及 data 先于 fmt 的合法布局。
    std::vector<uint8_t> extended(12); std::copy(bytes.begin(), bytes.begin() + 12, extended.begin());
    const std::vector<uint8_t> junk{'J','U','N','K',1,0,0,0,42,0};
    extended.insert(extended.end(), junk.begin(), junk.end());
    extended.insert(extended.end(), bytes.begin() + 36, bytes.end());
    extended.insert(extended.end(), bytes.begin() + 12, bytes.begin() + 36);
    put32(extended.data() + 4, uint32_t(extended.size() - 8));
    { std::ofstream out(bad, std::ios::binary | std::ios::trunc); out.write(reinterpret_cast<const char*>(extended.data()), extended.size()); }
    WavReader extra(bad); expect(extra.frames == 65 && extra.read(block, 64) == 64, "WAV chunk scanner");
    auto fifo = (std::filesystem::path(HOST_TEST_DIR) / "pipe").string();
    std::filesystem::remove(fifo); expect(::mkfifo(fifo.c_str(), 0600) == 0, "create FIFO fault input");
    auto begin = Clock::now();
    rejects([&] { WavReader invalid(fifo); }, "regular file");
    bool rejected = false;
    try { WavWriter invalid(fifo); } catch (const std::runtime_error&) { rejected = true; }
    expect(rejected && Clock::now() - begin < std::chrono::seconds(1), "FIFO opens fail without blocking");
    std::filesystem::remove(fifo);
}
}
int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string(argv[1]) == "--golden") {
            auto f = hello(); f.seq = 0x12345678; f.stamp = 0x0102030405060708ULL;
            static const char hex[] = "0123456789abcdef";
            for (auto b : encode(f)) std::cout << hex[b >> 4] << hex[b & 15];
            std::cout << '\n'; return 0;
        }
        if (argc == 2 && (std::string(argv[1]) == "--json" || std::string(argv[1]) == "--estimate-json")) {
            Status s; s.fail("quote\" slash\\ newline\n错误😀");
            if (std::string(argv[1]) == "--estimate-json") {
                s.record_alsa_queue_estimate(100, 1000, 48, 2000, 192);
                s.record_alsa_queue_estimate(101, 1000, 96, 2000, 480);
            }
            std::cout << s.json(); return 0;
        }
        if (argc == 2 && std::string(argv[1]) == "--replay") {
            packet_replay(); std::cout << checks << " replay checks passed\n"; return 0;
        }
        codec(); negotiation(); session_negotiation(); reception(); association(); queue_estimates(); packet_replay(); queues(); status(); cli(); wav(); trace_failure();
        std::cout << checks << " checks passed\n"; return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
