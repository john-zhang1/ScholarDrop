#!/usr/bin/env python
"""Drive the failure paths of docs/MANUAL-CHECKLIST.md against a running instance.

Items 1-7: wrong password, rate limit, expired session, denied collection,
encrypted PDF, oversized file, repository down, non-PDF. Every one of them is a
path where the honest thing to do is show a message, so what is being checked is
that a message is what appears -- not a traceback, not a blank page, not a lie.

    .venv/bin/python -m uvicorn --factory scholardrop.app:create_app --port 8100 &
    SCHOLARDROP_CHECK_USER=you@ou.edu SCHOLARDROP_CHECK_PASSWORD=... \\
      .venv/bin/python scripts/checklist.py --base-url http://127.0.0.1:8100 \\
      --instance local

Nothing here deposits. It creates at most one draft (item 4) and reports its id
so you can discard it.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
HTMX = {"HX-Request": "true"}

results: list[tuple[bool, str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    results.append((condition, label, detail))
    mark = f"{GREEN}  ok{RESET}" if condition else f"{RED}FAIL{RESET}"
    print(f"{mark} {label}{f' {DIM}— {detail}{RESET}' if detail else ''}")
    return condition


def step(title: str) -> None:
    print(f"\n{DIM}--- {title} ---{RESET}")


def encrypted_pdf() -> bytes:
    """A password-protected PDF, built rather than committed."""
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 144), "This document is encrypted.")
    buffer = io.BytesIO()
    document.save(
        buffer,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    document.close()
    return buffer.getvalue()


def plain_pdf(size: int = 2048) -> bytes:
    import fitz

    document = fitz.open()
    document.new_page().insert_text((72, 144), "A perfectly ordinary document.")
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    body = buffer.getvalue()
    return body + b"\n%" + b"x" * max(0, size - len(body))


def run(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    user = args.user or os.environ.get("SCHOLARDROP_CHECK_USER")
    password = os.environ.get("SCHOLARDROP_CHECK_PASSWORD")
    login_path = f"/login/{args.instance}"
    print(f"target: {base} (instance {args.instance})")

    with httpx.Client(base_url=base, follow_redirects=False, timeout=60.0) as web:
        if not (user and password):
            print(f"{YELLOW}no credentials (SCHOLARDROP_CHECK_USER/PASSWORD): stopping{RESET}")
            return 1

        step("2. signed in")
        r = web.post(login_path, data={"user": user, "password": password})
        if not check("sign-in works", r.status_code == 303, f"-> {r.status_code}"):
            print(r.text[:400])
            return 1

        step("3. denied collection (forged verified=1)")
        r = (
            web.get(f"/select/{args.denied_collection}?verified=1")
            if args.denied_collection
            else None
        )
        if r is not None:
            check("the pick is accepted by the UI", r.status_code in (303, 403))
            if r.status_code == 303:
                r = web.post(
                    "/submit",
                    files={"file": ("x.pdf", plain_pdf(), "application/pdf")},
                    headers=HTMX,
                )
                check(
                    "the repository refuses at create_workspaceitem",
                    r.status_code == 403 and "would not start a submission" in r.text,
                    f"-> {r.status_code}",
                )
        else:
            print(f"{YELLOW}skipped: pass --denied-collection <uuid> to check this{RESET}")

        step("4. encrypted PDF")
        uuids = re.findall(
            r"/select/([0-9a-f-]{36})\?verified=1", web.get("/browse/mine/results").text
        )
        if not check("a submittable collection exists", bool(uuids)):
            return 1
        web.get(f"/select/{uuids[0]}?verified=1")
        r = web.post(
            "/submit",
            files={"file": ("encrypted.pdf", encrypted_pdf(), "application/pdf")},
            headers=HTMX,
        )
        accepted = check("the upload is accepted", r.status_code == 200, f"-> {r.status_code}")
        draft = None
        if accepted:
            match = re.search(r"/submit/status/(\d+)", r.text)
            draft = match.group(1) if match else None
            for _ in range(40):
                if web.get(f"/submit/status/{draft}", headers=HTMX).status_code == 204:
                    break
                time.sleep(0.5)
            body = web.get(f"/review/{draft}").text
            check(
                "the form says extraction found nothing",
                "Nothing could be extracted" in body or "not been pre-filled" in body,
            )
            check("the form is still usable", 'name="traditionalpageone::dc.title"' in body)

        step("5. oversized file")
        r = web.post(
            "/submit",
            files={"file": ("huge.pdf", plain_pdf(args.max_bytes + 4096), "application/pdf")},
            headers=HTMX,
        )
        check(
            "refused with the limit named",
            r.status_code == 413 and "larger than" in r.text,
            f"-> {r.status_code}",
        )

        step("7. non-PDF renamed to .pdf")
        r = web.post(
            "/submit",
            files={"file": ("trojan.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")},
            headers=HTMX,
        )
        check("refused on content, not name", r.status_code == 400 and "not a PDF" in r.text)

        if draft:
            print(f"\n{YELLOW}left behind: draft {draft} — discard it at {base}/drafts{RESET}")

    step("2b. expired session mid-flow")
    print(f"{DIM}needs the server started with SCHOLARDROP_SESSION_TTL_SECONDS=1{RESET}")
    if args.session_ttl_is_short:
        with httpx.Client(base_url=base, follow_redirects=False, timeout=60.0) as web:
            web.post(login_path, data={"user": user, "password": password})
            time.sleep(2)
            r = web.get("/browse")
            check(
                "a page load returns to the picker",
                r.status_code == 303 and r.headers["location"] == "/",
            )
            r = web.get("/browse/mine/results", headers=HTMX)
            check(
                "an HTMX action redirects the browser instead of swapping a login form",
                r.status_code == 204 and r.headers.get("HX-Redirect") == "/",
            )
    else:
        print(f"{DIM}skipped (pass --session-ttl-is-short when it is){RESET}")

    step("6. repository down")
    print(f"{DIM}point the instance's base_url at a dead port and re-run item 2{RESET}")
    if args.dead_instance:
        with httpx.Client(base_url=base, follow_redirects=False, timeout=60.0) as web:
            r = web.post(f"/login/{args.dead_instance}", data={"user": user, "password": password})
            check(
                "sign-in says the repository could not be reached",
                r.status_code == 502 and "could not be reached" in r.text,
                f"-> {r.status_code}",
            )
    else:
        print(f"{DIM}skipped (pass --dead-instance <key>){RESET}")

    # Last, because the rate limiter is keyed by instance+IP: tripping it earlier
    # would lock out every sign-in the checks above depend on.
    step("1. wrong password, then the rate limit")
    with httpx.Client(base_url=base, follow_redirects=False, timeout=60.0) as web:
        r = web.post(login_path, data={"user": "nobody@example.invalid", "password": "wrong"})
        check("refused with a vague message", r.status_code == 401 and "not accepted" in r.text)
        check("does not reveal whether the account exists", "no such user" not in r.text.lower())
        codes = [
            web.post(
                login_path, data={"user": "nobody@example.invalid", "password": "wrong"}
            ).status_code
            for _ in range(6)
        ]
        check("throttled after repeated failures", 429 in codes, f"codes={codes}")

    failed = [label for ok, label, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  {RED}failed{RESET}: {label}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--instance", default="local", help="instance key in instances.yaml")
    parser.add_argument("--user")
    parser.add_argument("--denied-collection", help="uuid this account may NOT submit to")
    parser.add_argument("--dead-instance", help="instance key pointing at a dead port")
    parser.add_argument("--max-bytes", type=int, default=104857600)
    parser.add_argument("--session-ttl-is-short", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
