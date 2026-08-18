#!/usr/bin/env python3
"""Attacks: rule validate/critical-guard + KB alias FP/FN + retired-route absence.

Prep mode (default): if web/kb not delivered, print PREP and exit 2.
Live mode: pass --base http://host:port to hit APIs.

The five retired mutation routes (PUT /api/rules, POST /api/rules/reset,
POST /api/kb, DELETE /api/kb/{section}/{key}, POST /api/kb/reload) must now
return framework absence statuses (405/404); probes verify that. Rule
W1/W2 probes use the retained dry-run seam POST /api/rules/validate, which
never writes runtime_rules.yaml.

  .venv/bin/python scripts/attack_web_kb.py
  .venv/bin/python scripts/attack_web_kb.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _has_delivery() -> dict[str, bool]:
    return {
        "kb_pkg": (ROOT / "task4_consistency" / "kb").is_dir(),
        "web_pkg": (ROOT / "task4_consistency" / "web").is_dir()
        or (ROOT / "task4_consistency" / "api").is_dir()
        or any((ROOT / "scripts").glob("run_web*")),
        "run_web": (ROOT / "scripts" / "run_web.sh").is_file(),
    }


def _try_import_kb() -> Any | None:
    try:
        from task4_consistency import kb  # type: ignore

        return kb
    except Exception:
        try:
            from task4_consistency.kb import store  # type: ignore

            return store
        except Exception:
            return None


def attack_kb_local() -> list[tuple[str, str, str, bool]]:
    """Local KB attacks using official module API (isolated temp KB)."""
    import tempfile

    results: list[tuple[str, str, str, bool]] = []
    kb_mod = _try_import_kb()
    if kb_mod is None:
        results.append(("ADV-K*", "kb module", "not importable", False))
        return results

    add = getattr(kb_mod, "add_alias", None)
    remove = getattr(kb_mod, "remove_alias", None)
    list_sec = getattr(kb_mod, "list_section", None)
    reload = getattr(kb_mod, "reload_kb", None)
    get_kb = getattr(kb_mod, "get_kb", None)

    if not callable(add) or not callable(remove) or not callable(reload):
        results.append(
            (
                "ADV-K*",
                "kb API",
                f"missing exports add={callable(add)} remove={callable(remove)} reload={callable(reload)}",
                False,
            )
        )
        return results
    results.append(("ADV-K*", "kb API surface", "add_alias/remove_alias/reload_kb", True))

    # Isolate: temp KB file so we never poison configs/kb/entity_kb.json
    default_kb = ROOT / "configs" / "kb" / "entity_kb.json"
    with tempfile.TemporaryDirectory() as td:
        tmp_kb = Path(td) / "kb.json"
        tmp_kb.write_text(
            '{"version":1,"address_aliases":{},"org_aliases":{},"plate_prefixes":{}}',
            encoding="utf-8",
        )
        try:
            reload(tmp_kb)

            # ADV-K1: org_aliases collapse 一汽/上汽 → 大众 must NOT make brand rule consistent
            # (normalize_brand uses org_aliases but must keep JV prefixes distinct)
            add("org_aliases", "一汽大众汽车有限公司", "大众")
            add("org_aliases", "上汽大众汽车有限公司", "大众")
            from task4_consistency.models import Application, Document, FieldValue
            from task4_consistency.normalize.base import normalize_brand
            from task4_consistency.rules.engine import RuleEngine
            from task4_consistency.rules.loader import load_rules

            n1 = normalize_brand("一汽大众")
            n2 = normalize_brand("上汽大众")
            # even if full company names map via KB, short JV names must differ
            jv_ok = n1 != n2
            rules = load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
            eng = RuleEngine(rules)
            app = Application(
                "atk_k1",
                [
                    Document(
                        "d1",
                        "机动车登记证书",
                        {
                            "brand": FieldValue("一汽大众"),
                            "vin": FieldValue("LGXCE4CB0N0123456"),
                            "engine_no": FieldValue("E1"),
                            "owner_name": FieldValue("甲"),
                            "plate_no": FieldValue("苏A1"),
                            "reg_cert_no": FieldValue("RC"),
                            "reg_date": FieldValue("2024-01-01"),
                            "address": FieldValue("南京"),
                        },
                    ),
                    Document(
                        "d2",
                        "交强险保单",
                        {
                            "brand": FieldValue("上汽大众"),
                            "vin": FieldValue("LGXCE4CB0N0123456"),
                            "engine_no": FieldValue("E1"),
                            "insured_name": FieldValue("甲"),
                            "plate_no": FieldValue("苏A1"),
                            "plate_list": FieldValue("苏A1"),
                        },
                    ),
                    Document(
                        "d3",
                        "融资租赁合同",
                        {
                            "brand": FieldValue("一汽大众"),
                            "vin": FieldValue("LGXCE4CB0N0123456"),
                            "lessee_name": FieldValue("甲"),
                            "id_number": FieldValue("320102199001012016"),
                            "financed_amount": FieldValue("10000"),
                            "reg_cert_no": FieldValue("RC"),
                            "reg_date": FieldValue("2024-01-01"),
                        },
                    ),
                    Document(
                        "d4",
                        "发票",
                        {
                            "brand": FieldValue("上汽大众"),
                            "vin": FieldValue("LGXCE4CB0N0123456"),
                            "engine_no": FieldValue("E1"),
                            "invoice_amount": FieldValue("10000"),
                        },
                    ),
                    Document(
                        "d5",
                        "身份证",
                        {
                            "owner_name": FieldValue("甲"),
                            "id_number": FieldValue("320102199001012016"),
                            "address": FieldValue("南京"),
                        },
                    ),
                ],
            )
            rep = eng.run(app)
            v = next(c.verdict.value for c in rep.checks if c.rule_id == "R_BRAND_CROSS")
            results.append(
                (
                    "ADV-K1",
                    "brand JV via KB",
                    f"R_BRAND_CROSS={v} norm={n1!r}/{n2!r}",
                    v == "inconsistent" and jv_ok,
                )
            )
            remove("org_aliases", "一汽大众汽车有限公司")
            remove("org_aliases", "上汽大众汽车有限公司")

            # ADV-K2: unknown section "person" must reject (no silent person alias)
            person_ok = False
            try:
                add("person", "张三", "李四")
                detail = "accepted person section (hole)"
            except (KeyError, ValueError) as e:
                person_ok = True
                detail = f"rejected: {e}"
            from task4_consistency.normalize.person import normalize_person_name

            n1 = normalize_person_name("张三")
            n2 = normalize_person_name("李四")
            collapsed = n1 == n2 and n1 is not None
            results.append(
                (
                    "ADV-K2",
                    "person wrong alias",
                    f"{detail}; norm={n1!r}/{n2!r}",
                    person_ok and not collapsed,
                )
            )

            # ADV-K3/K10 regression: short key / cross-city still rejected
            short_ok = False
            try:
                add("address_aliases", "州", "X")
            except ValueError:
                short_ok = True
            city_ok = False
            try:
                add("address_aliases", "江苏苏州", "江苏南京")
            except ValueError:
                city_ok = True
            results.append(("ADV-K10", "short key reject", f"short_ok={short_ok}", short_ok))
            results.append(("ADV-K3", "cross-city reject", f"city_ok={city_ok}", city_ok))

            # surface: list_section works
            if callable(list_sec):
                sec = list_sec("org_aliases")
                results.append(("ADV-K-list", "list_section", f"type={type(sec).__name__}", isinstance(sec, dict)))
        finally:
            # restore default KB for other tests/processes
            try:
                reload(default_kb)
            except Exception:
                pass

    return results


def attack_web_http(base: str) -> list[tuple[str, str, str, bool]]:
    """HTTP attacks against rule/KB APIs (live server)."""
    import urllib.error
    import urllib.request

    results: list[tuple[str, str, str, bool]] = []
    base = base.rstrip("/")

    def req(method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        r = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return e.code, raw
        except Exception as e:
            return 0, str(e)

    # retired mutation routes must be absent: framework statuses only
    for hid, title, method, path, expect in [
        ("RET-1", "PUT /api/rules absent", "PUT", "/api/rules", 405),
        ("RET-2", "POST /api/rules/reset absent", "POST", "/api/rules/reset", 404),
        ("RET-3", "POST /api/kb absent", "POST", "/api/kb", 405),
        ("RET-4", "DELETE /api/kb/{s}/{k} absent", "DELETE", "/api/kb/org_aliases/x", 404),
        ("RET-5", "POST /api/kb/reload absent", "POST", "/api/kb/reload", 404),
    ]:
        code, _ = req(method, path)
        results.append((hid, title, f"HTTP {code} (expect {expect})", code == expect))

    # ADV-W1 bad rules payload (validate seam; never writes)
    code, _ = req(
        "POST",
        "/api/rules/validate",
        {"content": {"version": 1, "field_aliases": {}, "rules": [{"type": "exact"}]}},
    )
    results.append(
        ("ADV-W1", "bad rules reject", f"HTTP {code}", code >= 400 or code == 0)
    )

    # ADV-W2: try loosen R_VIN_CROSS (must 4xx under critical guard)
    code, rules_body = req("GET", "/api/rules")
    if code == 200 and isinstance(rules_body, dict):
        import copy

        content = copy.deepcopy(rules_body.get("content") or {})
        rules_list = content.get("rules") or []
        touched = False
        if isinstance(rules_list, list):
            for r in rules_list:
                if isinstance(r, dict) and r.get("id") == "R_VIN_CROSS":
                    r["on_missing"] = "skip"
                    r["require_all_docs"] = False
                    r["type"] = "fuzzy"
                    r["threshold"] = 0.0
                    touched = True
        if touched:
            code2, body2 = req("POST", "/api/rules/validate", {"content": content})
            ok = code2 >= 400
            results.append(
                (
                    "ADV-W2",
                    "loosen VIN FN",
                    f"validate={code2} detail={str(body2)[:80]}",
                    ok,
                )
            )
        else:
            results.append(
                ("ADV-W2", "loosen VIN FN", "R_VIN_CROSS not in GET content.rules", False)
            )
    else:
        results.append(("ADV-W2", "loosen VIN FN", f"GET /api/rules -> {code}", False))

    return results


def attack_w1_w2_local() -> list[tuple[str, str, str, bool]]:
    """Round16: in-process W1/W2 + retired-route probes via TestClient (no live server)."""
    import copy
    import tempfile

    import yaml
    from fastapi.testclient import TestClient

    from task4_consistency.web import app as webapp

    results: list[tuple[str, str, str, bool]] = []
    default_yaml = (ROOT / "configs" / "rules_auto_lease.yaml").read_text(encoding="utf-8")
    pkg = yaml.safe_load(default_yaml)

    with tempfile.TemporaryDirectory() as td:
        runtime = Path(td) / "runtime_rules.yaml"
        # patch runtime path for isolation
        old = webapp.RUNTIME_RULES
        webapp.RUNTIME_RULES = runtime
        try:
            client = TestClient(webapp.app)

            def validate(payload: dict[str, Any]) -> Any:
                return client.post("/api/rules/validate", json=payload)

            # retired mutation routes must be absent (framework statuses)
            for hid, title, method, path, body, expect in [
                ("RET-1", "PUT /api/rules absent", "put", "/api/rules",
                 {"content": {"version": 1}}, 405),
                ("RET-2", "POST /api/rules/reset absent", "post", "/api/rules/reset",
                 None, 404),
                ("RET-3", "POST /api/kb absent", "post", "/api/kb",
                 {"section": "org_aliases", "key": "x", "value": "y"}, 405),
                ("RET-4", "DELETE /api/kb/{s}/{k} absent", "delete",
                 "/api/kb/org_aliases/x", None, 404),
                ("RET-5", "POST /api/kb/reload absent", "post", "/api/kb/reload",
                 None, 404),
            ]:
                kwargs = {"json": body} if body is not None else {}
                res = getattr(client, method)(path, **kwargs)
                results.append(
                    (hid, title, f"HTTP {res.status_code} (expect {expect})", res.status_code == expect)
                )

            # ADV-W1: bad payload must 4xx and never create runtime
            code = validate(
                {"content": {"version": 1, "field_aliases": {}, "rules": [{"type": "exact", "field": "vin"}]}}
            ).status_code
            closed_w1 = code >= 400 and not runtime.exists()
            results.append(("ADV-W1", "bad rules zero-touch", f"HTTP {code} exists={runtime.exists()}", closed_w1))

            # ADV-W1b: poison yaml dry-run — no runtime to clobber under validate
            r = validate({"yaml_text": "not: valid: yaml: ["})
            results.append(
                (
                    "ADV-W1b",
                    "poison yaml dry-run",
                    f"HTTP {r.status_code} exists={runtime.exists()}",
                    r.status_code >= 400 and not runtime.exists(),
                )
            )

            # ADV-W1c: retained good package validates clean, still no write
            r = validate({"yaml_text": default_yaml})
            ok_v = (
                r.status_code == 200
                and r.json().get("ok") is True
                and not runtime.exists()
            )
            results.append(
                ("ADV-W1c", "good rules validate", f"HTTP {r.status_code} exists={runtime.exists()}", ok_v)
            )

            # ADV-W2: delete VIN
            loose = copy.deepcopy(pkg)
            loose["rules"] = [r for r in loose["rules"] if r.get("id") != "R_VIN_CROSS"]
            r = validate({"content": loose})
            err = (r.json().get("detail") or {}).get("error") if r.status_code >= 400 else None
            results.append(
                ("ADV-W2", "drop R_VIN_CROSS", f"HTTP {r.status_code} err={err}", r.status_code >= 400)
            )

            # ADV-W2b: type fuzzy
            loose = copy.deepcopy(pkg)
            for rule in loose["rules"]:
                if rule.get("id") == "R_VIN_CROSS":
                    rule["type"] = "fuzzy"
                    rule["threshold"] = 0.0
            r = validate({"content": loose})
            err = (r.json().get("detail") or {}).get("error") if r.status_code >= 400 else None
            results.append(
                (
                    "ADV-W2b",
                    "VIN type→fuzzy",
                    f"HTTP {r.status_code} err={err}",
                    r.status_code >= 400 and err == "critical_semantic_tamper",
                )
            )

            # ADV-W2c: strip docs
            loose = copy.deepcopy(pkg)
            for rule in loose["rules"]:
                if rule.get("id") == "R_VIN_CROSS":
                    rule["docs"] = []
            r = validate({"content": loose})
            err = (r.json().get("detail") or {}).get("error") if r.status_code >= 400 else None
            results.append(
                (
                    "ADV-W2c",
                    "VIN docs stripped",
                    f"HTTP {r.status_code} err={err}",
                    r.status_code >= 400 and err == "critical_docs_stripped",
                )
            )

            # ADV-W2d: on_missing skip
            loose = copy.deepcopy(pkg)
            for rule in loose["rules"]:
                if rule.get("id") == "R_VIN_CROSS":
                    rule["on_missing"] = "skip"
            r = validate({"content": loose})
            err = (r.json().get("detail") or {}).get("error") if r.status_code >= 400 else None
            results.append(
                (
                    "ADV-W2d",
                    "VIN on_missing=skip",
                    f"HTTP {r.status_code} err={err}",
                    r.status_code >= 400 and err == "critical_on_missing_skip",
                )
            )
        finally:
            webapp.RUNTIME_RULES = old

    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="Web base URL, e.g. http://127.0.0.1:8000")
    args = ap.parse_args()

    delivery = _has_delivery()
    print("=== delivery probe ===")
    for k, v in delivery.items():
        print(f"  {k}: {v}")

    if not any(delivery.values()) and not args.base:
        print("PREP: Web/KB not delivered yet. After delivery, POST /api/rules/validate is the retained dry-run validation endpoint and never mutates runtime rules.")
        print("After delivery: .venv/bin/python scripts/attack_web_kb.py --base http://127.0.0.1:8000")
        return 2

    results: list[tuple[str, str, str, bool]] = []
    if delivery["kb_pkg"] or _try_import_kb() is not None:
        results.extend(attack_kb_local())
    # Round16: always run in-process W1/W2 if web package present
    if delivery["web_pkg"]:
        results.extend(attack_w1_w2_local())
    if args.base:
        results.extend(attack_web_http(args.base))
    elif delivery["web_pkg"] or delivery["run_web"]:
        if not args.base:
            print("NOTE: live HTTP attacks need --base; ran in-process W1/W2 + retired-route probes")

    if not results:
        print("No runnable attacks (module shape unknown). Update script after API freeze.")
        return 2

    print("=== WEB/KB ATTACK RESULTS ===")
    open_n = 0
    for hid, title, detail, closed in results:
        st = "CLOSED" if closed else "OPEN"
        if not closed:
            open_n += 1
        print(f"{hid:12} | {st:6} | {title:22} | {detail}")
    print(f"open={open_n} total={len(results)}")
    # Issue #45 Round-1: any open result (W1/W2, KB, or RET) fails the gate
    return 0 if open_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
