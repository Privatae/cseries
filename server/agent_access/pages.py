"""The two pages a person sees: connecting an agent, and answering its request.

Every sentence on them is this Host's, written from its own records: the op
(a checked op name), the machine (one of the person's, tools.call checks it)
and the arguments (each quoted). What an agent wrote itself, its registered
name and its note on a request, never appears inside one of those sentences:
it stands in its own box, labelled as the agent's own words, escaped. No
script: each page is a form that posts back to itself.
"""
from __future__ import annotations

import html

_STYLE = ("body{font-family:system-ui,sans-serif;margin:2.5rem auto;max-width:34rem;padding:0 1rem;"
          "color:#1d1d1f;line-height:1.45}h1{font-size:1.35rem}q,.quote{background:#f3f3f5;"
          "border-radius:6px;padding:.1rem .35rem}.box{border:1px solid #d8d8dc;border-radius:8px;"
          "padding:1rem;margin:1rem 0}label{display:block;margin:.5rem 0}input[type=text]{font-size:1.1rem;"
          "padding:.4rem;width:9rem;letter-spacing:.15rem}button{font-size:1rem;padding:.5rem 1rem;"
          "margin:.4rem .4rem 0 0;border-radius:6px;border:1px solid #888;background:#fff}"
          "button.yes{background:#1d1d1f;color:#fff}.err{color:#a40000}.muted{color:#666;font-size:.9rem}")


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>{body}</body></html>")


def e(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _factor(code_hint: str) -> str:
    # Not `required`: a passkey check made on this Host in the last few
    # minutes confirms the person without a code (person.confirm).
    return ("<label>Code from your authenticator "
            "<input type=\"text\" name=\"code\" inputmode=\"numeric\" autocomplete=\"one-time-code\" "
            "maxlength=\"8\"></label>"
            f"<p class=\"muted\">{e(code_hint)} If you confirmed with your passkey on this Host in "
            "the last few minutes, you can leave it empty.</p>")


def message(title: str, text: str, *, error: bool = False) -> str:
    cls = " class=\"err\"" if error else ""
    return _page(title, f"<h1>{e(title)}</h1><p{cls}>{e(text)}</p>")


def agent_words(rows: list[tuple[str, str]]) -> str:
    """What the agent wrote itself, apart from the Host's sentences: one box,
    labelled as the agent's own words, each value quoted and escaped."""
    items = "".join(f"<dt>{e(label)}</dt><dd><q>{e(text)}</q></dd>" for label, text in rows if text)
    if not items:
        return ""
    return ("<div class=\"box agent-words\"><p class=\"muted\">In the agent's own words. Your Host "
            "has not checked them, and they are not your Host's.</p>"
            f"<dl>{items}</dl></div>")


# What the 'read' choice lets an agent do without asking, said exactly
# (tools.READ_GRANT_OPS and READ_GRANT_PREFIXES): nothing more is in it.
READ_WORDS = ("Without asking you, let it list your machines and run these on them: uptime; the "
              "health checks (disks, network, services, kernel, open ports, firewall, "
              "Kubernetes, alerts, metrics and system logs); what is installed, the folder "
              "names and the open windows' titles; and the lists of models, backups and your "
              "Host's ops with their status.")
NOT_READ_WORDS = ("Anything else asks you first, every time or once for good: reading your files, "
                  "your context store or anything else of yours, and every change.")


def connect_page(*, client_name: str, redirect_host: str, form_token: str,
                 error: str = "", read_checked: bool = True) -> str:
    err = f"<p class=\"err\">{e(error)}</p>" if error else ""
    checked = " checked" if read_checked else ""
    body = (
        "<h1>Connect an agent to your Host?</h1>"
        "<p>An agent asks to connect to your Host. When you approve, your Host sends its "
        f"sign-in to the address the agent registered, at <b>{e(redirect_host)}</b>.</p>"
        f"{agent_words([('What it calls itself', client_name)])}"
        "<form method=\"post\" class=\"box\">"
        f"<input type=\"hidden\" name=\"form\" value=\"{e(form_token)}\">"
        f"<label><input type=\"checkbox\" name=\"read\" value=\"1\"{checked}> {e(READ_WORDS)}</label>"
        f"<p class=\"muted\">{e(NOT_READ_WORDS)} You can disconnect it in Settings, under "
        "Connected agents.</p>"
        f"{_factor('Your Host asks for it so that nobody but you can connect an agent.')}"
        f"{err}"
        "<button class=\"yes\" name=\"decision\" value=\"approve\">Connect</button>"
        "<button name=\"decision\" value=\"deny\" formnovalidate>Don't connect</button>"
        "</form>")
    return _page("Connect an agent", body)


def grant_ask_page(*, client_name: str, server: str, op: str, args: list, note: str,
                   form_token: str, error: str = "", reads: bool = False) -> str:
    """`reads`: the op is one the box classes as a read that the 'read'
    choice does not cover (the person's files, their context store): the page
    says it reads, never that it changes."""
    err = f"<p class=\"err\">{e(error)}</p>" if error else ""
    args_html = ("<p>With these arguments: " + ", ".join(f"<q>{e(a)}</q>" for a in args) + "</p>"
                 if args else "")
    head = "An agent asks to read from your machine" if reads else "An agent asks to make a change"
    what = ("It would read what this returns from the machine, and the agent would receive it."
            if reads else "")
    body = (
        f"<h1>{e(head)}</h1>"
        f"<p>A connected agent asks your Host to run <b>{e(op)}</b> on <b>{e(server)}</b>.</p>"
        f"{args_html}"
        + (f"<p>{e(what)}</p>" if what else "")
        + f"{agent_words([('Its name', client_name), ('Its reason', note)])}"
        "<p class=\"muted\">Nothing has run. If you allow it, your Host runs it through the same "
        "checks as its own work, and the machine still applies its own rules.</p>"
        "<form method=\"post\" class=\"box\">"
        f"<input type=\"hidden\" name=\"form\" value=\"{e(form_token)}\">"
        f"{_factor('Needed to allow it, either way.')}"
        f"{err}"
        "<button class=\"yes\" name=\"decision\" value=\"once\">Just this once</button>"
        f"<button class=\"yes\" name=\"decision\" value=\"always\">Always for this agent</button>"
        "<button name=\"decision\" value=\"refused\" formnovalidate>No</button>"
        "</form>"
        f"<p class=\"muted\">\"Always\" lets this agent run {e(op)} on {e(server)} again without "
        "asking. You can take it back in Settings, under Connected agents.</p>")
    return _page("An agent's request", body)


def machine_ask_page(*, client_name: str, server: str, op: str, where: str,
                     form_token: str = "", card_words: str = "", error: str = "") -> str:
    """A request the machine itself holds for the person's approval: the card
    the Host put up for it (card_words, its own sitrep; the person's click
    sends the ask on to their approval device, as the chat's pop-up does), or
    the device or console the machine named (where)."""
    err = f"<p class=\"err\">{e(error)}</p>" if error else ""
    if card_words:
        form = ("<form method=\"post\" class=\"box\">"
                f"<p>{e(card_words)}</p>"
                f"<input type=\"hidden\" name=\"form\" value=\"{e(form_token)}\">{err}"
                "<button class=\"yes\" name=\"decision\" value=\"once\">Do it once</button>"
                "<button name=\"decision\" value=\"no\">Don't run it</button>"
                "</form>")
    else:
        form = f"<div class=\"box\"><p>{e(where)}</p></div>{err}"
    body = (
        "<h1>A machine is waiting for your approval</h1>"
        f"<p>A connected agent asked your Host to run <b>{e(op)}</b> on <b>{e(server)}</b>. "
        f"{e(server)} runs it only once you approve it.</p>"
        f"{agent_words([('Its name', client_name)])}{form}")
    return _page("A machine's approval", body)
