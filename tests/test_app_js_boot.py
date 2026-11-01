"""O app.js inteiro precisa rodar até o fim sem erro no carregamento.

Um erro no nível de cima do script (ex.: usar uma const antes da linha em que ela é criada) para tudo: o boot
nunca roda, a intro só sai pelo limite de 12 s e o dashboard fica nos esqueletos. Aqui o script roda no Node
com um DOM de mentira (todo elemento existe e aceita qualquer coisa); só os erros síncronos do carregamento
contam, porque são eles que derrubam a página."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.config import ROOT

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[2], "utf8");
const stub = new Proxy(function () {}, {
  get(target, key) {
    if (key === Symbol.iterator) return function* () {};
    if (key === Symbol.toPrimitive) return () => 0;
    if (key === "then") return () => stub;         // promessa que nunca resolve: o assíncrono fica parado
    if (key === "length" || key === "size") return 0;
    if (key === "matches") return false;
    return stub;
  },
  set() { return true; },
  apply() { return stub; },
  construct() { return stub; },
  has() { return true; },
});
const sandbox = {
  console, Intl, JSON, Math, Date, Map, Set, WeakMap, Promise, Number, String, Array, Object, Symbol, RegExp, Error,
  encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN, URLSearchParams, URL, setTimeout: () => 0,
  clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {}, requestAnimationFrame: () => 0,
  queueMicrotask: () => {}, document: stub, navigator: stub, localStorage: stub, sessionStorage: stub, location: stub,
  history: stub, matchMedia: () => stub, getComputedStyle: () => stub, fetch: () => new Promise(() => {}),
  Chart: stub, EventSource: undefined, Notification: stub, CSS: stub, performance: { now: () => 0 },
  addEventListener: () => {}, removeEventListener: () => {}, FormData: stub, Blob: stub, Image: stub,
  IntersectionObserver: stub, ResizeObserver: stub, MutationObserver: stub, atob: () => "", btoa: () => "",
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
process.on("unhandledRejection", () => {});      // erro dentro de promessa não derruba o carregamento
try {
  vm.runInNewContext(code, sandbox, { filename: "app.js" });
  console.log("OK");
} catch (error) {
  console.log("ERRO " + String(error) + " @ " + String(error && error.stack || "").split("\n").slice(0, 2).join(" | "));
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="sem Node")
def test_app_js_loads_to_the_end(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(ROOT / "static/js/app.js")], capture_output=True, text=True,
                         timeout=60).stdout.strip().splitlines()
    assert out and out[-1] == "OK", "\n".join(out[-5:])
