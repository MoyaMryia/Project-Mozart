#!/usr/bin/env python
# nllb_service.py — NLLB-200 (CTranslate2) 翻译服务
# 对外暴露 OpenAI /v1/chat/completions 兼容接口，作为 llama-server 的即插即用替换。
# 协议只做翻译：取最后一条 user 消息 → MT → OpenAI 响应格式返回。
#
# 依赖: ~/mozart-archive/nllb_env (ctranslate2-cuda + transformers + sentencepiece)
# 模型: ~/mozart-archive/nllb-ct2 (int8) + ~/mozart-archive/nllb-tok (tokenizer)
#
# 用法:
#   nllb_env/bin/python tools/nllb_service.py [--port 18200] [--device cuda]
#       [--compute int8] [--src zho_Hans] [--tgt eng_Latn] [--threads 4]

import argparse
import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ctranslate2
import transformers

MODEL_DIR = "/home/moyamryia/mozart-archive/nllb-ct2"
TOKENIZER_DIR = "/home/moyamryia/mozart-archive/nllb-tok"

_args = None
_tok = None
_model = None


def load_model():
    global _tok, _model
    _tok = transformers.AutoTokenizer.from_pretrained(TOKENIZER_DIR, src_lang=_args.src)
    _model = ctranslate2.Translator(MODEL_DIR, device=_args.device,
                                    compute_type=_args.compute)
    # 预热：首请求常带 CUDA 上下文/算子初始化开销
    b = _tok.convert_ids_to_tokens(_tok.encode("预热"))
    _model.translate_batch([b], target_prefix=[[_tgt_token()]], beam_size=_args.beam)
    print(f"[nllb] ready: {_args.device}/{_args.compute} src={_args.src} tgt={_args.tgt}",
          flush=True)


def _tgt_token():
    return _tok.convert_ids_to_tokens(_tok.convert_tokens_to_ids([_args.tgt]))[0]


def translate(text: str) -> str:
    src = _tok.convert_ids_to_tokens(_tok.encode(text.strip()))
    r = _model.translate_batch([src], target_prefix=[[_tgt_token()]],
                               beam_size=_args.beam, max_decoding_length=512)
    ids = _tok.convert_tokens_to_ids(r[0].hypotheses[0])
    return _tok.decode(ids, skip_special_tokens=True).strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # 静默默认访问日志
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"status": "ok", "model": "nllb-200-distilled-600M",
                             "device": _args.device, "compute": _args.compute})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        msgs = req.get("messages", [])
        text = next((m["content"] for m in reversed(msgs)
                     if m.get("role") == "user"), "")
        if isinstance(text, list):  # 兼容 OpenAI 分段 content
            text = "".join(p.get("text", "") for p in text)
        t0 = time.monotonic()
        try:
            out = translate(text)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        dt = (time.monotonic() - t0) * 1000
        print(f"[nllb] {dt:.0f}ms  {text[:40]!r} -> {out[:60]!r}", flush=True)
        self._json(200, {
            "id": "nllb", "object": "chat.completion", "model": "nllb-200-600M",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": out}}],
            "usage": {"latency_ms": dt},
        })


def main():
    global _args
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18200)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--src", default="zho_Hans")
    ap.add_argument("--tgt", default="eng_Latn")
    ap.add_argument("--beam", type=int, default=2)
    ap.add_argument("--threads", type=int, default=4)
    _args = ap.parse_args()
    load_model()
    srv = ThreadingHTTPServer(("127.0.0.1", _args.port), Handler)
    print(f"[nllb] listening on 127.0.0.1:{_args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
