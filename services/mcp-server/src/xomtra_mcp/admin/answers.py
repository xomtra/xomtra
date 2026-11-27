"""Admin page Answers: the answer guidelines sets (ADR 090).

Factory sets come with the code and are read only; custom sets are kept in the audit
database with their history. One set is active for the installation; the MCP server
reads it for every result that carries ``answer_guidelines`` (cache 30 s). The core
rules are shown but cannot be changed. User text is always escaped and sits in
``<textarea>``, ``<pre>`` or ``<bdi>``, which the GUI translation leaves alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from xomtra_mcp.admin.html import e, fmt, page, table
from xomtra_mcp.admin.view import prefix
from xomtra_mcp.answer_sets import AnswerSetStore, AnswerSetStoreError, StoredSet
from xomtra_mcp.audit import AuditError
from xomtra_mcp.guidelines import (
    CORE_GUIDELINES,
    CUSTOM_SETS_MAX,
    DEFAULT_SET_ID,
    DESCRIPTION_MAX,
    FACTORY_BY_ID,
    FACTORY_SETS,
    NAME_MAX,
    TEXT_MAX,
    AnswerSet,
    check_set,
    clean_text,
    compose,
)

TITLE = "Answers"
EXPORT_FORMAT = 1
# A form with a whole set (8,000 characters, percent-encoded) or an import of 20 sets.
FORM_LIMIT = 1_000_000

Audit = Callable[..., Awaitable[None]]
Form = Callable[..., Awaitable[dict[str, str]]]


class RefusedError(ValueError):
    """A request the page refuses with a message (400)."""


def _name(answer_set: AnswerSet) -> str:
    # Factory names are part of the GUI (translated); custom names are user text.
    return (
        e(answer_set.name) if answer_set.id in FACTORY_BY_ID else f"<bdi>{e(answer_set.name)}</bdi>"
    )


def _hidden(name: str, value: str) -> str:
    return f"<input type=hidden name={name} value='{e(value)}'>"


def _post(action: str, fields: str, button: str, cls: str = "filters") -> str:
    return (
        f"<form class={cls} method=post action='{prefix()}/answers'>"
        f"{_hidden('action', action)}{fields}{button}</form>"
    )


class AnswersPage:
    def __init__(self, store: AnswerSetStore | None, audit: Audit, form: Form) -> None:
        self.store = store
        self._audit = audit
        self._form = form

    # ---- reading ----------------------------------------------------------------

    def _load(self) -> tuple[list[StoredSet], str]:
        assert self.store is not None  # noqa: S101 - checked by the callers
        custom = self.store.custom()
        active = self.store.active()
        active_id = active.set_id if active else DEFAULT_SET_ID
        if active_id not in FACTORY_BY_ID and all(s.set.id != active_id for s in custom):
            active_id = DEFAULT_SET_ID  # a deleted custom set: the server uses the default
        return custom, active_id

    def _find(self, custom: list[StoredSet], set_id: str) -> AnswerSet | None:
        if set_id in FACTORY_BY_ID:
            return FACTORY_BY_ID[set_id]
        return next((s.set for s in custom if s.set.id == set_id), None)

    # ---- page -------------------------------------------------------------------

    async def show(
        self,
        request: Request,
        user: dict[str, Any],
        *,
        note: str = "",
        status_code: int = 200,
        selected: str | None = None,
        draft: AnswerSet | None = None,
        preview: AnswerSet | None = None,
    ) -> Response:
        username = user.get("username")
        if self.store is None:
            body = "<p class=warn>The audit database is not configured for the admin GUI.</p>"
            return HTMLResponse(page(TITLE, body, username))
        try:
            custom, active_id = await asyncio.to_thread(self._load)
            chosen = selected or request.query_params.get("set") or active_id
            current = self._find(custom, chosen) or self._find(custom, active_id)
            assert current is not None  # noqa: S101 - the active set always resolves
            versions = (
                await asyncio.to_thread(self.store.versions, current.id, 30)
                if current.id not in FACTORY_BY_ID
                else []
            )
            activations = await asyncio.to_thread(self.store.activations, 20)
        except AnswerSetStoreError:
            body = "<p class=bad>The answer sets store is unavailable.</p>"
            return HTMLResponse(page(TITLE, body, username), 503)
        body = note + self._intro() + self._sets(custom, active_id, current.id)
        body += self._editor(draft or current, active_id, custom)
        if preview is not None:
            body += (
                "<h3>Preview: what the assistant gets</h3>"
                f"<pre class=set>{e(compose(preview))}</pre>"
            )
        if versions:
            body += self._versions(current, versions)
        body += self._activations(activations, custom) + self._transfer(len(custom))
        return HTMLResponse(page(TITLE, body, username), status_code)

    @staticmethod
    def _intro() -> str:
        return (
            "<p class=warn><strong>Style of the answers</strong>"
            "<span class=more>"
            "By default xomtra asks the assistant for relevant answers with charts or tables "
            "and a friendly tone. Choose another set below or write your own; a change "
            "applies within 30 seconds, without a restart."
            "</span><span class=more>"
            "Core rules always apply: no jokes about accidents or threats to life and "
            "health, nothing sexist, racist, homophobic, xenophobic, political or religious."
            "</span></p>"
            "<p class=muted>xomtra gives the assistant the core rules and the active set "
            "with the results of server_info, get_findings and get_weekly_summary; nothing "
            "comes from xomtra.com. The sets are guidance for the assistant, not a security "
            "boundary: roles, hidden entities, read only access and the audit are enforced "
            "by xomtra itself.</p>"
            "<details><summary>Core rules (always apply, cannot be changed)</summary>"
            f"<pre class=set>{e(CORE_GUIDELINES)}</pre></details>"
        )

    def _sets(self, custom: list[StoredSet], active_id: str, shown: str) -> str:
        rows = []
        items: list[tuple[AnswerSet, datetime | None, str | None]] = [
            (s, None, None) for s in FACTORY_SETS
        ]
        items += [(s.set, s.changed_at, s.changed_by) for s in custom]
        for answer_set, changed, by in items:
            is_active = answer_set.id == active_id
            name = _name(answer_set)
            if is_active:
                name += " <span class=badge>active</span>"
            open_link = (
                "shown below"
                if answer_set.id == shown
                else f"<a href='{prefix()}/answers?set={e(answer_set.id)}'>Open</a>"
            )
            activate = (
                ""
                if is_active
                else _post("activate", _hidden("set", answer_set.id), "<button>Activate</button>")
            )
            kind = "factory" if answer_set.id in FACTORY_BY_ID else "custom"
            description = (
                e(answer_set.description)
                if kind == "factory"
                else f"<bdi>{e(answer_set.description)}</bdi>"
            )
            rows.append(
                [
                    name,
                    kind,
                    description,
                    len(answer_set.text),
                    f"{fmt(changed)}<br>{e(by)}" if changed else "",
                    open_link,
                    activate,
                ]
            )
        new = _post(
            "create",
            f"<input name=name required maxlength={NAME_MAX} placeholder='name of a new set'>",
            "<button>New empty set</button>",
        )
        return (
            "<h2>Sets</h2>"
            + table(
                ["set", "kind", "description", "characters", "changed", "", ""],
                rows,
                {3},
                trusted_html={0, 2, 4, 5, 6},
            )
            + (new if len(custom) < CUSTOM_SETS_MAX else "")
        )

    def _editor(self, current: AnswerSet, active_id: str, custom: list[StoredSet]) -> str:
        factory = current.id in FACTORY_BY_ID
        head = f"<h2>{_name(current)}</h2>"
        copy_name = f"{current.name} (copy)"[:NAME_MAX]
        duplicate = (
            _post(
                "duplicate",
                _hidden("set", current.id)
                + f"<input name=name required maxlength={NAME_MAX} value='{e(copy_name)}'>",
                "<button>Duplicate</button>",
            )
            if len(custom) < CUSTOM_SETS_MAX
            else f"<p class=muted>At most {CUSTOM_SETS_MAX} custom sets.</p>"
        )
        if factory:
            text = current.text or "(no answer guidelines: the assistant gets only the core rules)"
            return (
                head + "<p class=muted>A factory set comes with xomtra and changes with updates. "
                "Duplicate it to write your own.</p>"
                + "<details><summary>Text of the set</summary>"
                + f"<pre class=set>{e(text)}</pre></details>"
                + duplicate
            )
        newer = ""
        source = FACTORY_BY_ID.get(current.based_on or "")
        if source is not None and (current.based_on_version or 0) < source.version:
            newer = (
                "<p class=warn>The factory set this one was copied from has a newer "
                "version. Open it to compare and copy what you need.</p>"
            )
        edit = (
            f"<form class=set method=post action='{prefix()}/answers'>"
            + _hidden("set", current.id)
            + "<label>name <input name=name required "
            f"maxlength={NAME_MAX} value='{e(current.name)}'></label>"
            + f"<input name=description maxlength={DESCRIPTION_MAX} "
            f"value='{e(current.description)}' placeholder='description (optional)'>"
            + f"<textarea class=set name=text maxlength={TEXT_MAX}>{e(current.text)}</textarea>"
            + f"<span class=muted>At most {TEXT_MAX} characters, plain text. Write it in "
            "English or in your language; the assistant answers in the user's language. "
            "The core rules come first and cannot be loosened by the set.</span>"
            + "<div><button name=action value=save>Save</button> "
            "<button name=action value=preview>Preview</button></div></form>"
        )
        delete = (
            ""
            if current.id == active_id
            else _post(
                "delete",
                _hidden("set", current.id)
                + "<label><input type=checkbox name=confirm value=1 required> delete this set"
                "</label>",
                "<button class=danger>Delete</button>",
            )
        )
        return head + newer + edit + duplicate + delete

    @staticmethod
    def _versions(current: AnswerSet, versions: list[Any]) -> str:
        rows = [
            [
                v.changed_at,
                v.changed_by,
                f"<bdi>{e(v.name)}</bdi>",
                len(v.text),
                _post(
                    "restore",
                    _hidden("set", current.id) + _hidden("version", str(v.id)),
                    "<button value=reset>Restore</button>",
                )
                if i
                else "current",
            ]
            for i, v in enumerate(versions)
        ]
        return "<h2>History of this set</h2>" + table(
            ["when", "by", "name", "characters", ""], rows, {3}, trusted_html={2, 4}
        )

    def _activations(self, activations: list[Any], custom: list[StoredSet]) -> str:
        if not activations:
            return ""
        rows = []
        for a in activations:
            found = self._find(custom, a.set_id)
            name = _name(found) if found else "deleted set"
            rows.append([a.changed_at, a.changed_by, name])
        return "<h2>Activations</h2>" + table(["when", "by", "set"], rows, trusted_html={2})

    @staticmethod
    def _transfer(count: int) -> str:
        imp = (
            _post(
                "import",
                "<textarea class='set import' name=json required "
                'placeholder=\'{"xomtra_answer_sets": 1, "sets": [...]}\'></textarea>',
                "<div><button>Import as new sets</button></div>",
                "set",
            )
            if count < CUSTOM_SETS_MAX
            else ""
        )
        return (
            "<h2>Export and import</h2>"
            f"<p><a href='{prefix()}/answers/export'>Download custom sets (JSON)</a></p>" + imp
        )

    # ---- changes ----------------------------------------------------------------

    async def post(self, request: Request, user: dict[str, Any]) -> Response:
        if self.store is None:
            return Response(status_code=404)
        form = await self._form(request, FORM_LIMIT)
        action = form.get("action", "")
        by = user.get("username")
        try:
            if action == "preview":
                draft = self._draft(form)
                return await self.show(
                    request, user, selected=form.get("set"), draft=draft, preview=draft
                )
            selected = await asyncio.to_thread(self._apply, action, form, by)
            await self._audit(
                f"answers_{action}",
                user,
                arguments={k: v for k, v in form.items() if k in ("set", "name", "version")},
            )
        except RefusedError as err:
            set_id = form.get("set", "")
            # A refused edit keeps what was typed, so nothing is lost.
            kept = (
                AnswerSet(
                    set_id, form.get("name", ""), form.get("text", ""), form.get("description", "")
                )
                if action in ("save", "preview") and set_id not in FACTORY_BY_ID
                else None
            )
            return await self.show(
                request,
                user,
                note=f"<p class=bad>{e(err)}</p>",
                status_code=400,
                selected=form.get("set"),
                draft=kept,
            )
        except (AnswerSetStoreError, AuditError):
            return await self.show(
                request,
                user,
                note="<p class=bad>Not saved: the answer sets store or audit is unavailable.</p>",
                status_code=503,
            )
        target = f"?set={selected}" if selected else ""
        return RedirectResponse(f"{prefix()}/answers{target}", status_code=303)

    def _draft(self, form: dict[str, str]) -> AnswerSet:
        set_id = form.get("set", "")
        if set_id in FACTORY_BY_ID:
            return FACTORY_BY_ID[set_id]
        name, description = form.get("name", ""), form.get("description", "")
        text = clean_text(form.get("text", ""))
        problems = check_set(name, description, text)
        if problems:
            raise RefusedError(" ".join(problems))
        return AnswerSet(set_id, name, text, description)

    def _unique(self, name: str, custom: list[StoredSet], own: str | None = None) -> None:
        taken = {s.name.lower() for s in FACTORY_SETS}
        taken |= {s.set.name.lower() for s in custom if s.set.id != own}
        if name.lower() in taken:
            raise RefusedError(f"A set called {name} exists already.")

    def _apply(self, action: str, form: dict[str, str], by: str | None) -> str | None:  # noqa: PLR0911, PLR0912 - one branch per action
        assert self.store is not None  # noqa: S101 - checked by the caller
        custom, active_id = self._load()
        set_id = form.get("set", "")
        found = self._find(custom, set_id)
        if action == "activate":
            if found is None:
                raise RefusedError("Unknown set.")
            self.store.activate(set_id, by)
            return set_id
        if action in ("create", "duplicate", "import") and len(custom) >= CUSTOM_SETS_MAX:
            raise RefusedError(f"At most {CUSTOM_SETS_MAX} custom sets.")
        if action == "create":
            name = form.get("name", "")
            if problems := check_set(name, "", ""):
                raise RefusedError(" ".join(problems))
            self._unique(name, custom)
            return self.store.create(name, "", "", by=by)
        if action == "duplicate":
            if found is None:
                raise RefusedError("Unknown set.")
            name = form.get("name", "")
            if problems := check_set(name, found.description, found.text):
                raise RefusedError(" ".join(problems))
            self._unique(name, custom)
            factory = found.id in FACTORY_BY_ID
            return self.store.create(
                name,
                found.description,
                found.text,
                by=by,
                based_on=found.id if factory else found.based_on,
                based_on_version=found.version if factory else found.based_on_version,
            )
        if action in ("save", "delete", "restore") and (found is None or set_id in FACTORY_BY_ID):
            raise RefusedError("Factory sets cannot be changed; duplicate one first.")
        if action == "save":
            draft = self._draft(form)
            self._unique(draft.name, custom, own=set_id)
            self.store.update(set_id, draft.name, draft.description, draft.text, by=by)
            return set_id
        if action == "delete":
            if set_id == active_id:
                raise RefusedError("The active set cannot be deleted; activate another one first.")
            if form.get("confirm") != "1":
                raise RefusedError("Tick the box to confirm.")
            self.store.delete(set_id, by)
            return None
        if action == "restore":
            versions = {str(v.id): v for v in self.store.versions(set_id, 1000)}
            version = versions.get(form.get("version", ""))
            if version is None:
                raise RefusedError("Unknown version.")
            self._unique(version.name, custom, own=set_id)
            self.store.update(set_id, version.name, version.description, version.text, by=by)
            return set_id
        if action == "import":
            return self._import(form.get("json", ""), custom, by)
        raise RefusedError("Unknown action.")

    def _import(self, raw: str, custom: list[StoredSet], by: str | None) -> str | None:
        assert self.store is not None  # noqa: S101 - checked by the caller
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise RefusedError("Not valid JSON.") from err
        if not isinstance(data, dict) or data.get("xomtra_answer_sets") != EXPORT_FORMAT:
            raise RefusedError("Not an export of xomtra answer sets.")
        sets = data.get("sets")
        if not isinstance(sets, list):
            raise RefusedError("Not an export of xomtra answer sets.")
        if len(custom) + len(sets) > CUSTOM_SETS_MAX:
            raise RefusedError(f"At most {CUSTOM_SETS_MAX} custom sets.")
        names = {s.name.lower() for s in FACTORY_SETS} | {s.set.name.lower() for s in custom}
        parsed = []
        for item in sets:
            if not isinstance(item, dict):
                raise RefusedError("Not an export of xomtra answer sets.")
            name = str(item.get("name", "")).strip()
            description = str(item.get("description", "")).strip()
            text = clean_text(str(item.get("text", "")))
            base, n = name, 2
            while name.lower() in names:
                suffix = f" ({n})"
                name, n = base[: NAME_MAX - len(suffix)] + suffix, n + 1
            if problems := check_set(name, description, text):
                raise RefusedError(f"{base}: " + " ".join(problems))
            names.add(name.lower())
            parsed.append((name, description, text))
        last = None
        for name, description, text in parsed:
            last = self.store.create(name, description, text, by=by)
        return last

    async def export(self, _: Request, user: dict[str, Any]) -> Response:
        if self.store is None:
            return Response(status_code=404)
        try:
            custom, active_id = await asyncio.to_thread(self._load)
        except AnswerSetStoreError:
            return Response("The answer sets store is unavailable.", status_code=503)
        active = self._find(custom, active_id)
        body = {
            "xomtra_answer_sets": EXPORT_FORMAT,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "active": active.name if active else None,
            "sets": [
                {"name": s.set.name, "description": s.set.description, "text": s.set.text}
                for s in custom
            ],
        }
        return Response(
            json.dumps(body, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="xomtra-answer-sets.json"'},
        )
