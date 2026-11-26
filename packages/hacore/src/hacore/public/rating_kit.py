"""Materials for the usefulness rating (preregistration ``docs/evaluation/usefulness-prereg.md``,
sections 3 and 6): the blinded description of a finding, the forms of the human panel,
the requests of the AI raters and the adjudication of the panel's ratings.

Blinding: a rater sees what a finding says (the series, the direction, the period, the
observed and expected value and the context facts of the home) and never how strong it
is or where a ranking puts it. ``describe`` reads only the fields in ``SHOWN``; the
strength, evidence, score, p- and q-values, the rulebook's verdicts (usefulness,
category, triviality) and the method's structure flags stay out, and every form and
request is in a random order of its own. A test checks this.

    python -m hacore.public.rating_kit forms --sample sample.csv --out <dir> --raters a,b --lang cs
    python -m hacore.public.rating_kit requests --sample sample.csv --model <id> --out ai.jsonl
    python -m hacore.public.rating_kit adjudicate --ratings <dir> --out adjudication.csv

``requests`` writes one Message Batches request per finding and role (temperature 0, the
model id given exactly); ``parse`` turns the batch results into ratings.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np

# Fields of a sampled finding a rater may see. Everything else of the sample CSV is
# hidden (strength, evidence, score, q, rulebook verdicts, weights, strata, the set).
SHOWN = (
    "finding_type",
    "entity",
    "related",
    "lag_hours",
    "flags",
    "period_start",
    "period_end",
    "observed",
    "expected",
)
# The sign of the score is the direction of a relation or a shift; its size is hidden.
SIGN_OF = "score"
# Context facts of the home or the data are shown in plain words ([flags] of the rubric);
# flags that are the method's own judgement have no text there and stay hidden.
LANGS = ("en", "cs", "de")
ORDER_SEED = 20261126
FORM_COLUMNS = ("order", "sample_id", "finding", "usefulness", "trivial", "category", "note")
CATEGORIES = ("fault", "savings", "automation", "behaviour", "none")


@dataclass(frozen=True, slots=True)
class Rubric:
    version: str
    data: dict[str, Any]

    @property
    def roles(self) -> list[str]:
        return [r["name"] for r in self.data["role"]]

    def text(self) -> str:
        """The rubric as raters read it (the same words on the form and in the prompt)."""
        d = self.data
        lines = [d["intro"].strip(), "", d["usefulness"]["question"]]
        lines += [f"  {v['value']} {v['label']}: {v['text']}" for v in d["usefulness"]["levels"]]
        lines += ["", d["trivial"]["question"], "  " + " ".join(d["trivial"]["text"].split())]
        lines += ["", d["category"]["question"]]
        lines += [f"  {v['label']}: {v['text']}" for v in d["category"]["values"]]
        return "\n".join(lines)

    def form_text(self) -> str:
        """The rubric and how to fill in the form, for the human panel."""
        return f"{self.text()}\n\n{self.data['form']['help']}"

    def normalise(self, usefulness: str, trivial: str, category: str) -> tuple[int, bool, str]:
        """An answer of the form in this language -> (1-4, trivial, category code);
        raises ``ValueError`` outside the rubric."""
        u = int(usefulness.strip())
        if u not in (1, 2, 3, 4):
            raise ValueError(f"usefulness {u}")
        t = trivial.strip().lower()
        form = self.data["form"]
        if t not in {form["yes"], form["no"]}:
            raise ValueError(f"trivial {trivial!r}")
        c = category.strip().lower()
        codes = {
            key.lower(): v["value"]
            for v in self.data["category"]["values"]
            for key in (v["value"], v["label"])
        }
        if c not in codes:
            raise ValueError(f"category {category!r}")
        return u, t == form["yes"], str(codes[c])

    def system_prompt(self, role: str) -> str:
        role_text = next(r["text"] for r in self.data["role"] if r["name"] == role)
        return f"{role_text}\n\n{self.text()}\n\n{self.data['answer'].strip()}"


def _rules(name: str) -> dict[str, Any]:
    return tomllib.loads(resources.files("hacore").joinpath(f"rules/{name}").read_text("utf-8"))


def load_rubric(lang: str = "en") -> Rubric:
    """The rubric; ``lang`` cs or de gives the panel's wording over the English source
    (the roles and the answer format of the AI raters stay English)."""
    data = _rules("usefulness-rubric.toml")
    if lang != "en":
        if lang not in LANGS:
            raise ValueError(f"language {lang!r}")
        local = _rules(f"usefulness-rubric.{lang}.toml")
        if local["version"] != data["version"]:
            raise ValueError(f"rubric {lang} {local['version']} != {data['version']}")
        data = data | local
    return Rubric(str(data["version"]), data)


def _name(entity: str | None, house: str | None) -> str:
    """Channel name without the domain and the house prefix of the public datasets."""
    name = (entity or "").split(".", 1)[-1]
    if house and name.startswith(f"{house}_"):
        name = name[len(house) + 1 :]
    return name.replace("_", " ")


def _num(v: Any, words: Mapping[str, str]) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return words["unknown"]
    return f"{x:.3g}".replace(".", words["decimal"])


def _time(v: Any) -> str:
    """``YYYY-MM-DD HH:MM`` of a time stamp (UTC in the public datasets)."""
    return str(v or "")[:16].replace("T", " ")


def _sign(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get(SIGN_OF) or 0.0)
    except ValueError:
        return 0.0


def describe(row: Mapping[str, Any], rubric: Rubric | None = None) -> str:
    """What the finding says, in plain words of the rubric's language, from the fields
    in ``SHOWN`` and the sign of the score only."""
    d = (rubric or load_rubric()).data
    words, flags = d["describe"], d["flags"]
    house = str(row.get("house") or "")
    kind = row.get("finding_type")
    start, end = _time(row.get("period_start")), _time(row.get("period_end"))
    values = {
        "a": _name(row.get("entity"), house),
        "b": _name(row.get("related"), house),
        "start": start,
        "end": end,
        "day": start[:10],
        "observed": _num(row.get("observed"), words),
        "expected": _num(row.get("expected"), words),
    }
    if kind == "correlation":
        lag = int(float(row.get("lag_hours") or 0))
        values["how"] = words["rises"] if _sign(row) >= 0 else words["falls"]
        values["when"] = words["later"].format(lag=lag) if lag else words["same_time"]
        text = words["relation"].format(**values)
    elif kind == "anomaly":
        text = words["anomaly"].format(**values)
    else:
        text = words["change"].format(**values)
    facts = [flags[f] for f in str(row.get("flags") or "").split() if f in flags]
    return " ".join([text, *facts])


def _order(n: int, key: str) -> list[int]:
    """A random order of ``n`` items of its own for every rater or role."""
    seed = [ORDER_SEED, *key.encode()]
    return [int(i) for i in np.random.default_rng(seed).permutation(n)]


def forms(
    rows: Sequence[Mapping[str, Any]], raters: Iterable[str], rubric: Rubric | None = None
) -> dict[str, list[dict[str, Any]]]:
    """One blinded form per rater: the findings in an order of the rater's own (the
    same in every language), with empty answer columns."""
    rubric = rubric or load_rubric()
    out: dict[str, list[dict[str, Any]]] = {}
    for rater in raters:
        out[rater] = [
            {
                "order": n + 1,
                "sample_id": rows[i]["sample_id"],
                "finding": describe(rows[i], rubric),
                "usefulness": "",
                "trivial": "",
                "category": "",
                "note": "",
            }
            for n, i in enumerate(_order(len(rows), f"human:{rater}"))
        ]
    return out


def requests(
    rows: Sequence[Mapping[str, Any]], rubric: Rubric, model: str, max_tokens: int = 300
) -> list[dict[str, Any]]:
    """Message Batches requests: one per finding and role, one finding per call,
    temperature 0, in an order of each role's own."""
    out = []
    for role in rubric.roles:
        for i in _order(len(rows), f"ai:{role}"):
            out.append(
                {
                    "custom_id": f"{rows[i]['sample_id']}-{role}",
                    "params": {
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": 0,
                        "system": rubric.system_prompt(role),
                        "messages": [{"role": "user", "content": describe(rows[i], rubric)}],
                    },
                }
            )
    return out


@dataclass(frozen=True, slots=True)
class Rating:
    sample_id: str
    rater: str
    usefulness: int
    trivial: bool
    category: str


def parse_answer(text: str) -> tuple[int, bool, str]:
    """The JSON object of an AI answer; raises ``ValueError`` when it breaks the rubric."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    obj = json.loads(text[start : end + 1])
    u, t, c = obj.get("usefulness"), obj.get("trivial"), obj.get("category")
    if u not in (1, 2, 3, 4) or not isinstance(t, bool) or c not in CATEGORIES:
        raise ValueError(f"outside the rubric: {obj}")
    return int(u), t, str(c)


def parse_results(lines: Iterable[str]) -> tuple[list[Rating], list[str]]:
    """Ratings from Message Batches results (JSONL); the ids that failed or broke the
    rubric are returned, not dropped silently."""
    ratings: list[Rating] = []
    failed: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line)
        cid = str(item["custom_id"])
        sample_id, _, role = cid.rpartition("-")
        result = item.get("result") or {}
        if result.get("type") != "succeeded":
            failed.append(cid)
            continue
        try:
            text = "".join(
                b.get("text", "") for b in result["message"]["content"] if b.get("type") == "text"
            )
            u, t, c = parse_answer(text)
        except (ValueError, KeyError, TypeError):
            failed.append(cid)
            continue
        ratings.append(Rating(sample_id, f"ai:{role}", u, t, c))
    return ratings, failed


@dataclass(frozen=True, slots=True)
class Adjudication:
    result: int | None
    rule: str


def adjudicate(first: Sequence[int], revote: Sequence[int] | None = None) -> Adjudication:
    """Panel consensus of three independent ratings (prereg section 3): spread <= 1 ->
    median; spread >= 2 -> discussion and a new independent vote (``revote``), majority
    of 2 of 3, else its median. ``result`` is None while a needed revote is missing.
    No rater has a casting vote."""
    if len(first) != 3:
        raise ValueError("three ratings")
    if max(first) - min(first) <= 1:
        return Adjudication(int(statistics.median(first)), "median")
    if revote is None:
        return Adjudication(None, "discuss")
    if len(revote) != 3:
        raise ValueError("three ratings in the revote")
    value, count = max(((v, list(revote).count(v)) for v in set(revote)), key=lambda x: x[1])
    if count >= 2:
        return Adjudication(int(value), "majority")
    return Adjudication(int(statistics.median(revote)), "revote_median")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _human(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r.get("human", "").lower() in {"true", "1"}]


def adjudication_table(forms_by_rater: Mapping[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    """``adjudication.csv`` from the three filled forms: the ratings, the result or
    ``discuss``. The revote and the reason are filled in by the panel."""
    raters = sorted(forms_by_rater)
    if len(raters) != 3:
        raise ValueError("three raters")
    by_id: dict[str, dict[str, int]] = {}
    for rater in raters:
        for r in forms_by_rater[rater]:
            if r.get("usefulness", "").strip():
                by_id.setdefault(r["sample_id"], {})[rater] = int(r["usefulness"])
    out = []
    for sid in sorted(by_id):
        got = by_id[sid]
        row: dict[str, Any] = {"sample_id": sid, **{f"rating_{r}": got.get(r, "") for r in raters}}
        if len(got) == 3:
            a = adjudicate([got[r] for r in raters])
            row |= {"result": "" if a.result is None else a.result, "rule": a.rule}
        else:
            row |= {"result": "", "rule": "missing"}
        row |= {"revote": "", "reason": ""}
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.public.rating_kit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("forms", help="blinded CSV forms of the human panel")
    f.add_argument("--sample", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--raters", required=True, help="comma-separated, three for the panel")
    f.add_argument("--all", action="store_true", help="every finding, not only the human sample")
    f.add_argument("--lang", choices=LANGS, default="en", help="language of the form")
    r = sub.add_parser("requests", help="Message Batches requests of the AI raters")
    r.add_argument("--sample", type=Path, required=True)
    r.add_argument("--model", required=True, help="exact model id")
    r.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("parse", help="ratings from Message Batches results")
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = sub.add_parser("adjudicate", help="adjudication.csv from three filled forms")
    a.add_argument("--ratings", type=Path, required=True, help="directory of form-<rater>.csv")
    a.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rubric = load_rubric()
    if args.cmd == "forms":
        rows = _read_csv(args.sample)
        rows = rows if args.all else _human(rows)
        args.out.mkdir(parents=True, exist_ok=True)
        local = load_rubric(args.lang)
        for rater, form in forms(rows, args.raters.split(","), local).items():
            _write_csv(args.out / f"form-{rater}.csv", form, FORM_COLUMNS)
        (args.out / "rubric.txt").write_text(
            f"Rubric {local.version} ({args.lang})\n\n{local.form_text()}\n", encoding="utf-8"
        )
        print(json.dumps({"rubric": local.version, "lang": args.lang, "findings": len(rows)}))
    elif args.cmd == "requests":
        reqs = requests(_read_csv(args.sample), rubric, args.model)
        with args.out.open("w", encoding="utf-8") as out:
            for req in reqs:
                out.write(json.dumps(req, ensure_ascii=False) + "\n")
        print(json.dumps({"rubric": rubric.version, "model": args.model, "requests": len(reqs)}))
    elif args.cmd == "parse":
        ratings, failed = parse_results(args.results.read_text("utf-8").splitlines())
        _write_csv(
            args.out,
            [
                {
                    "sample_id": x.sample_id,
                    "rater": x.rater,
                    "usefulness": x.usefulness,
                    "trivial": x.trivial,
                    "category": x.category,
                }
                for x in ratings
            ],
            ("sample_id", "rater", "usefulness", "trivial", "category"),
        )
        print(json.dumps({"ratings": len(ratings), "failed": failed}))
    else:
        filled = {
            path.stem.removeprefix("form-"): _read_csv(path)
            for path in sorted(args.ratings.glob("form-*.csv"))
        }
        table = adjudication_table(filled)
        raters = sorted(filled)
        columns = [
            "sample_id",
            *(f"rating_{r}" for r in raters),
            "result",
            "rule",
            "revote",
            "reason",
        ]
        _write_csv(args.out, table, columns)
        print(
            json.dumps(
                {"findings": len(table), "discuss": sum(t["rule"] == "discuss" for t in table)}
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
