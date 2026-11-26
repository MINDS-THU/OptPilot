"""Dependency-free local web interface for the tutorial Resource."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>OptPilot package anatomy</title>
<style>
body{margin:0;font:16px/1.5 system-ui;background:#f6f4f8;color:#24212a}main{max-width:900px;margin:auto;padding:48px 24px}
.eyebrow{color:#6d357a;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{font-size:clamp(2rem,5vw,4rem);line-height:1;margin:.2em 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin-top:32px}.card{background:white;border:1px solid #ded7e3;border-radius:18px;padding:20px;box-shadow:0 14px 36px #3d244012}
.n{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;background:#efe5f2;color:#61316c;font-weight:800}code{color:#61316c}p{color:#645d6b}
</style><main><div class="eyebrow">Build your first package</div><h1>Four pieces, one useful Run</h1>
<p>Read these cards in order, then open the files in a Workspace and change the toy factory.</p><section class="grid">
<article class="card"><span class="n">1</span><h2>Environment</h2><p>Defines the simulator, Candidate shape, and metrics.</p><code>environment.yaml</code></article>
<article class="card"><span class="n">2</span><h2>Method</h2><p>Proposes Candidates that match the Environment contract.</p><code>method.yaml</code></article>
<article class="card"><span class="n">3</span><h2>Run setup</h2><p>Pairs them and declares the objective, budget, and seed.</p><code>find_best_settings.yaml</code></article>
<article class="card"><span class="n">4</span><h2>Resource</h2><p>Adds a reusable interface or headless action—this page is one.</p><code>optpilot.resource.yaml</code></article>
</section></main></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 3000), Handler).serve_forever()
