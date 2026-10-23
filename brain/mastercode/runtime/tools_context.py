"""The context tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller


async def _exec_dcn(args: dict) -> dict:
    """The Distributed Context Network — governed access to the USER's own stores
    Ops: stores | status | propose_store | list | stat |
    read_range. Every read returns a TYPED envelope (ok/offline/refused/gone) —
    disclose a non-ok state, never skip it silently. There is no delete op."""
    a = args or {}
    op = (a.get("op") or "stores").strip().lower()
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "dcn failed: brain not initialized"}
        eid = brain_service._entity_id

        if op in ("stores", "status"):
            from brain.mastercode import dcn
            stores = await dcn.list_stores(pool, eid)
            note = ("per-store sweep freshness lands with the indexers (CN-2); "
                    "grant status is live" if op == "status" else
                    "cite store + path in answers — the route map's answer IS the citation")
            return {"status": "ok", "result": {"stores": stores, "count": len(stores),
                                               "note": note}}

        if op == "propose_store":
            from server.network import nac_propose
            res = await nac_propose.propose_dcn_store(
                pool, eid, store_class=a.get("class") or a.get("store_class"),
                server=a.get("server"), root=a.get("root"),
                bucket=a.get("bucket"), prefix=a.get("prefix"),
                endpoint=a.get("endpoint"), cred_name=a.get("cred_name"),
                mask=a.get("mask"), excludes=a.get("excludes"),
                sweep=a.get("sweep") or "hourly",
                display_name=a.get("display_name"), note=a.get("note"),
                sensitive=bool(a.get("sensitive")),
                legacy_portal_id=a.get("legacy_portal_id"))
            if res.get("revived"):
                # A revoked store at this location was revived as a fresh draft.
                res["next"] = ("This location was revoked; re-proposing revived it as "
                               "a fresh DRAFT — the user re-approves it in Context "
                               "Manager (or POST /nac/dcn/{id}/approve) with their "
                               "step-up code. You cannot approve it.")
            elif res.get("already_exists"):
                # No draft was created — a live store already exists at this
                # location. Don't tell the user to go approve a draft that isn't there.
                res["next"] = ("This location is already in the DCN; nothing to "
                               "approve. If it shows a stale status, manage it in "
                               "Context Manager.")
            else:
                res["next"] = ("DRAFT created — the user approves it in Context Manager "
                               "(or POST /nac/dcn/{id}/approve) with their step-up code. "
                               "You cannot approve it; you cannot produce their code.")
            return {"status": "ok", "result": res}

        # ── Move plans: the one write lane, archive-only. ──
        if op == "propose_plan":
            from server.network import nac_propose
            res = await nac_propose.propose_dcn_move_plan(
                pool, eid, store_id=a.get("store_id"), moves=a.get("moves"),
                archive=a.get("archive") or "Archive", title=a.get("title"), note=a.get("note"))
            res["next"] = ("DRAFT plan created — the user approves it in Context Manager (the "
                           "store card) with their step-up code; you cannot. Then op='run_plan' "
                           "mode='dry_run' to check every move on their machine, and mode='apply' "
                           "to execute with rollback + verify. Nothing is deleted: 'remove' is a "
                           f"move into '{res.get('archive')}'.")
            return {"status": "ok", "result": res}
        if op == "plans":
            from server.network import nac_commit
            res = await nac_commit.dcn_move_plans_list(pool, eid, store_id=a.get("store_id"))
            return {"status": "ok", "result": res}
        if op == "run_plan":
            from server.network import nac_dcn
            res = await nac_dcn.run_move_plan(pool, eid, a.get("plan_id"), a.get("mode") or "dry_run")
            if res.get("state") == "ok" and res.get("mode") == "dry_run":
                res["next"] = ("ready: true means every move passed the machine's check — ask the user "
                               "before op='run_plan' mode='apply'; problems list what to fix in a new plan."
                               if res.get("ready") else
                               "Fix the listed problems in a NEW plan (propose_plan); this one cannot be edited.")
            elif res.get("mode") == "apply":
                res["next"] = ("Report it as it is: applied / verified / mismatches. The machine keeps the "
                               "same report at <root>/.agent/plans/<plan_id>.json.")
            return {"status": "ok", "result": res}

        if op in ("list", "stat", "read_range", "search", "catalog"):
            from server.network import nac_dcn
            start_byte, end_byte = a.get("start_byte"), a.get("end_byte")
            if op == "read_range":
                # Byte positions are integers, a start is never negative, and
                # the CLAMPED start is what the lane is asked for: a -5 that
                # reached the cabinet as -5 made its eof test say the object
                # had ended.
                start = _whole_arg(start_byte, 0)
                asked = _whole_arg(end_byte, None)
                if start is _NOT_WHOLE or asked is _NOT_WHOLE:
                    return _refused("start_byte and end_byte must be whole numbers of bytes "
                        f"(got start_byte={start_byte!r}, end_byte={end_byte!r})")
                start = max(0, start)
                # A byte position past what a store can address is refused here:
                # the machine box reads positions as Go int64 (client-go/dcn.go
                # dcnOpArgs) and answers "bad json args" past 2^63-1.
                if start > _INT64_MAX or (asked is not None and asked > _INT64_MAX):
                    return _refused("start_byte and end_byte must be at most 2^63-1 "
                        f"({_INT64_MAX}), the largest byte position a store can address")
                enc = a.get("encoding")
                # One check for every reader: an unknown name, a codec
                # that is not text, or one that carries state between pages.
                why = file_access_codec_refusal(enc) if enc not in (None, "") else None
                if why:
                    return _refused(why)
                # Sized to what one page of THIS file costs on the wire, for
                # this call's window — here, at the Host-tool boundary, never
                # inside run_dcn_op, whose background readers keep the lane's
                # full range. end <= start means "the page from start".
                from mastercode.runtime.tool_result_wall import page_fetch_size
                cap = max(1, min(nac_dcn.DCN_MAX_RANGE_BYTES,
                                 page_fetch_size(nac_dcn.page_key(a.get("store_id"), a))))
                if asked is None or asked <= start:
                    asked = start + cap
                start_byte, end_byte = start, min(asked, start + cap, _INT64_MAX)
            dcn_args = {
                "path": a.get("path"), "prefix": a.get("prefix"),
                "start_byte": start_byte, "end_byte": end_byte,
                "limit": a.get("limit"), "encoding": a.get("encoding"),
                # bastion agent lane: a semantic query, or a document id from a hit
                "query": a.get("query"), "k": a.get("k"), "doc": a.get("doc"),
                "kind": a.get("kind"), "offset": a.get("offset")}
            page_args = dict(dcn_args)
            res = await nac_dcn.run_dcn_op(pool, eid, a.get("store_id"), op, dcn_args)
            if (op == "read_range" and not a.get("doc") and nac_dcn.needs_byte_order(a.get("encoding"))
                    and isinstance(res, dict) and res.get("state") == "ok"):
                # utf-16/32/8-sig: the byte order (and BOM) is the FILE's, read
                # from its head. Page 1 settles it from its OWN bytes;
                # a later page uses what page 1 found while this page can still
                # be that file (same size, no NUL pattern of the other order),
                # else the head is read again (4 KiB) — the path may now hold
                # another file.
                pkey = nac_dcn.page_key(a.get("store_id"), a)
                conc = nac_dcn.remembered_codec(pkey, res) if start_byte > 0 else None
                if conc is None:
                    head_res = res
                    if start_byte > 0:
                        head_res = await nac_dcn.run_dcn_op(
                            pool, eid, a.get("store_id"), op,
                            {**dcn_args, "start_byte": 0, "end_byte": nac_dcn.HEAD_BYTES})
                        if not isinstance(head_res, dict) or head_res.get("state") != "ok":
                            return {"status": "ok", "result": head_res}
                    conc = nac_dcn.resolve_head(a.get("encoding"), head_res)
                    if isinstance(conc, str):
                        return _refused(conc)
                    nac_dcn.remember_codec(pkey, conc, res)
                page_args["_codec"] = conc
            if op == "read_range":
                # A reader page: text or base64 with offset/next_offset/eof, so
                # even a page the wall cuts names its resume point.
                res = nac_dcn.reader_page(res, page_args)
            if op == "catalog" and res.get("state") == "ok":
                res["next"] = ("These are the bastion agent's cards — the MAP of the store: per directory "
                               "(default) files, bytes, newest change, type mix, keywords, entity counts; "
                               "per file (kind='files') title, head, keywords, entities. It is PAGED: read "
                               "`total` and `next_offset`, and pass prefix/offset/limit for a big store. Plan from this "
                               "(archive, cleanup, migration) before reading any file; text in cards is "
                               "sanitized ([PII_*] stays as is). There is no delete op — a plan is advice "
                               "the user acts on.")
            if op == "search" and res.get("state") == "ok":
                res["next"] = ("Hits are SANITIZED windows ([PII_*] placeholders stand for "
                               "redacted personal data — keep them as they are). To read "
                               "more of a hit, call op='read_range' with its `doc` id and "
                               "start_byte/end_byte; cite store + doc as provenance.")
            return {"status": "ok", "result": res}

        return _refused(f"unknown dcn op '{op}' (stores|status|propose_store|list|stat|"
                          "read_range|search|catalog|propose_plan|plans|run_plan — there is no "
                          "delete; the operation does not exist)")
    except ValueError as e:
        return _refused(str(e))
    except Exception as e:
        detail = getattr(e, "detail", None)
        return _refused_if_caller(e, f"dcn failed: {detail or e}")


_NOT_WHOLE = object()
_INT64_MAX = 2 ** 63 - 1


def file_access_codec_refusal(encoding):
    """file_access.codec_refusal, the one encoding check every reader uses."""
    from server.capabilities.file_access import codec_refusal
    return codec_refusal(encoding)


def _whole_arg(v, default):
    """A byte position from a tool call: None/'' -> default; an int, a whole
    float (3.0) or a numeric string -> int; anything else (a bool, 1.5, 'abc',
    a list) -> _NOT_WHOLE, so the call is answered with an error instead of
    being truncated or ignored."""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return _NOT_WHOLE
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else _NOT_WHOLE
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return _NOT_WHOLE
    return _NOT_WHOLE
