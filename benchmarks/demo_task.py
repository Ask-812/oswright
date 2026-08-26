"""
The demo task: transcribe an invoice into an expense system.

Calculator proves nothing. Anyone evaluating a desktop agent wants to see work
they recognise, and data transcription between two systems is the single most
automated task in the world -- it is what RPA licences are sold for. A human
does it by reading one window and typing into another, which is exactly the
loop an agent has to reproduce.

It is also the honest place to show perception cost. The task is eight fields,
so an agent that re-reads the whole screen after every action pays for that
screen eight times. That is the entire argument for this project, and a
four-click Calculator sum is too short to show it.

Deliberate design choices:

**The two systems use different names for the same thing.** The invoice says
`Invoice No`, the form says `Reference`; the invoice says `Total Due`, the form
says `Amount (USD)`. Real systems never agree on vocabulary, and a fixture where
the labels match on both sides would be measuring string equality rather than
transcription.

**The page grades itself from the DOM.** On submit it compares what is actually
in the inputs against the expected values and writes the verdict into
`document.title`. That verdict comes from the application's own state, not from
anything oswright perceived -- the same rule the benchmarks follow, because a
perception layer cannot be allowed to mark its own homework.

**Nothing real is touched.** It is a local HTML file in a throwaway Chrome
profile: no network, no account, no saved state, nothing to lose.
"""

import http.server
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subjects as S  # noqa: E402

#: What the agent has to move across. Values appear on the left of the page and
#: must end up in the correspondingly named field on the right.
ENTRIES = [
    ("Vendor",        "Northwind Traders"),
    ("Reference",     "INV-4417"),
    ("Service Date",  "2026-08-14"),
    ("Purchase Order", "PO-88231"),
    ("Amount",        "4182.50"),
    ("Cost Centre",   "CC-2140"),
    ("Category",      "Software Licences"),
    ("Approver",      "R. Whitfield"),
]

FORM_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>oswright demo ready</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", sans-serif; background: #eef1f5; margin: 0;
         color: #1b2733; }
  .bar { background: #1f3a5f; color: #fff; padding: 14px 26px; font-size: 19px;
         font-weight: 600; letter-spacing: .2px; }
  .bar span { opacity: .65; font-weight: 400; font-size: 15px; margin-left: 14px; }
  .wrap { display: flex; gap: 22px; padding: 22px 26px; align-items: flex-start; }
  .card { background: #fff; border: 1px solid #ccd4de; border-radius: 6px;
          padding: 20px 24px; }
  .doc { width: 470px; }
  .doc h2 { margin: 0 0 4px; font-size: 21px; }
  .doc .sub { color: #667; font-size: 15px; margin-bottom: 16px; }
  .row { display: flex; justify-content: space-between; padding: 9px 0;
         border-bottom: 1px solid #eef1f5; font-size: 17px; }
  .row .k { color: #566; }
  .row .v { font-weight: 600; }
  .total { margin-top: 14px; padding-top: 12px; border-top: 2px solid #1f3a5f;
           display: flex; justify-content: space-between; font-size: 20px;
           font-weight: 700; }
  .form { flex: 1; }
  .form h2 { margin: 0 0 18px; font-size: 21px; }
  .field { display: flex; align-items: center; margin-bottom: 13px; }
  label { width: 168px; font-size: 17px; color: #344; cursor: pointer; }
  input { flex: 1; font-size: 17px; padding: 9px 11px; border: 1px solid #b9c3cf;
          border-radius: 4px; background: #fdfdfe; font-family: inherit; }
  input:focus { outline: 2px solid #2f6fd0; border-color: #2f6fd0; }
  button { margin-top: 10px; font-size: 18px; font-weight: 600; padding: 12px 34px;
           background: #1f6feb; color: #fff; border: 0; border-radius: 5px;
           cursor: pointer; }
  #verdict { margin-top: 14px; font-size: 18px; font-weight: 700; min-height: 24px; }
  .ok { color: #147a3d; } .bad { color: #b3261e; }
</style></head>
<body>
<div class="bar">Expense System <span>&rsaquo; New entry &rsaquo; unsubmitted</span></div>
<div class="wrap">

  <div class="card doc">
    <h2>Northwind Traders</h2>
    <div class="sub">Scanned original &mdash; received 15 Aug</div>
    <div class="row"><span class="k">Invoice No</span><span class="v">INV-4417</span></div>
    <div class="row"><span class="k">Issued</span><span class="v">2026-08-14</span></div>
    <div class="row"><span class="k">Order Ref</span><span class="v">PO-88231</span></div>
    <div class="row"><span class="k">Charge To</span><span class="v">CC-2140</span></div>
    <div class="row"><span class="k">Line Item</span><span class="v">Software Licences</span></div>
    <div class="row"><span class="k">Signed Off</span><span class="v">R. Whitfield</span></div>
    <div class="total"><span>Total Due</span><span>USD 4182.50</span></div>
  </div>

  <div class="card form">
    <h2>Record this expense</h2>
    <div class="field"><label for="f0">Vendor</label><input id="f0"></div>
    <div class="field"><label for="f1">Reference</label><input id="f1"></div>
    <div class="field"><label for="f2">Service Date</label><input id="f2"></div>
    <div class="field"><label for="f3">Purchase Order</label><input id="f3"></div>
    <div class="field"><label for="f4">Amount</label><input id="f4"></div>
    <div class="field"><label for="f5">Cost Centre</label><input id="f5"></div>
    <div class="field"><label for="f6">Category</label><input id="f6"></div>
    <div class="field"><label for="f7">Approver</label><input id="f7"></div>
    <button id="save" onclick="save()">Submit expense</button>
    <div id="verdict"></div>
  </div>

</div>
<script>
// The expected values live here so the page can check its own inputs. This is
// the application's account of what happened, which is the only thing allowed
// to grade the run -- oswright's own output never decides whether it passed.
const EXPECTED = %s;

function save() {
  const wrong = [];
  EXPECTED.forEach((pair, i) => {
    const got = (document.getElementById('f' + i).value || '').trim();
    if (got !== pair[1]) wrong.push(pair[0]);
  });
  const v = document.getElementById('verdict');
  if (wrong.length === 0) {
    document.title = 'oswright demo SAVED-OK';
    v.className = 'ok';
    v.textContent = 'Saved. 8 of 8 fields match the invoice.';
  } else {
    document.title = 'oswright demo SAVED-MISMATCH ' + wrong.join(',');
    v.className = 'bad';
    v.textContent = 'Mismatch: ' + wrong.join(', ');
  }
}
</script>
</body></html>
"""


class ExpenseEntry(S.Chrome):
    """
    The demo application: a local, throwaway line-of-business form.

    Served over loopback rather than opened as a `file://` URL. Two reasons: a
    `file://` path puts the machine's account name in the address bar, which has
    no business being in a published recording, and a localhost URL is what
    software of this kind actually looks like.
    """

    name = "Expense System"
    WINDOW_HINT = "oswright demo"
    click_settle_s = 0.35
    PAGE = FORM_PAGE % str([[k, v] for k, v in ENTRIES]).replace("'", '"')

    def __init__(self):
        super().__init__()
        self._httpd = None
        self._port = None

    def page_url(self) -> str:
        """Serve the page from loopback, on whatever port is free."""
        body = self.PAGE.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # a benchmark does not need an access log

        # Port 0 lets the OS pick a free one, so two runs cannot collide.
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._port}/expenses/new"

    def cleanup(self, window):
        super().cleanup(window)
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def succeeded(self, window) -> bool:
        return "SAVED-OK" in (self.ground_truth(window) or "")

    def verdict(self, window) -> str:
        title = self.ground_truth(window) or ""
        if "SAVED-OK" in title:
            return "all 8 fields match"
        if "SAVED-MISMATCH" in title:
            return "mismatch: " + title.split("SAVED-MISMATCH", 1)[1].strip()
        return "never submitted"
