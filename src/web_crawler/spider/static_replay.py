"""Compile static agent traces into deterministic, offline Spider projects.

The supported slice is deliberately narrow: successful GET ``navigate`` records
with response snapshots followed by CSS ``extract`` records. Other actions are
rejected at compile time instead of being silently skipped. Compilation writes
cached responses plus a runnable Spider project; execution is offline-only and
uses the same extraction contract and persistent item deduplication on every
run. Low-confidence or invalid items are quarantined before feed export.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..ai.recorder import ActionRecord
from ..exceptions import ConfigError
from ..response import Response
from ..spider.cache import CacheMiddleware
from ..spider.feed import FeedExportPipeline
from ..spider.spider import Request, Spider
from ..spider.validation import ValidationPipeline

__all__ = ["StaticReplayCompiler", "compile_static_replay"]

_SECRET_KEY_RE = re.compile(
    r"authorization|cookie|credential|password|secret|session|token", re.IGNORECASE
)
_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def _redact_text(value: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def _redact_url(url: str, secrets: list[str]) -> str:
    parts = urlsplit(_redact_text(url, secrets))
    query = urlencode(
        [
            (
                key,
                "[REDACTED]" if _SECRET_KEY_RE.search(key) else _redact_text(value, secrets),
            )
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _sanitize_headers(headers: Any, secrets: list[str]) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ConfigError("response.headers must be a mapping")
    return {
        str(key): (
            "[REDACTED]" if _SECRET_KEY_RE.search(str(key)) else _redact_text(str(value), secrets)
        )
        for key, value in headers.items()
    }


def _sanitize_response(response: Any, url: str, secrets: list[str]) -> tuple[bytes, int]:
    if not isinstance(response, dict):
        raise ConfigError("navigate response must be a mapping with body_base64")
    try:
        body = base64.b64decode(response["body_base64"], validate=True)
        status = int(response.get("status", 200))
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ConfigError("navigate response needs valid body_base64 and status") from exc
    if not 200 <= status < 300:
        raise ConfigError(f"static replay only caches 2xx responses, got {status}")
    for secret in secrets:
        if secret:
            body = body.replace(secret.encode("utf-8"), b"[REDACTED]")
    return body, status


class StaticReplayCompiler:
    """Compile recorded static responses and CSS extraction into an offline project."""

    def compile(
        self,
        records: list[ActionRecord],
        output_dir: str | Path,
        *,
        secrets: list[str] | tuple[str, ...] = (),
    ) -> Path:
        """Write a runnable project and return its directory.

        Only successful GET navigation snapshots and CSS extraction are supported.
        Every extraction must resolve exactly one root and each declared field at
        record time so adaptive replay has a concrete fingerprint. Unknown actions,
        missing snapshots, non-GET methods and ambiguous selectors fail compilation.
        Secret values and sensitive header/query values are redacted before writing.
        """
        target = Path(output_dir)
        if target.exists() and any(target.iterdir()):
            raise ConfigError(f"replay output directory must be empty: {target}")
        secret_values = [str(secret) for secret in secrets if secret]
        plans: list[dict[str, Any]] = []
        responses: list[tuple[str, bytes, int, dict[str, str]]] = []
        seen_urls: set[str] = set()
        current_url = ""
        current_step = -1

        for record in records:
            if not record.success or record.action_type == "done":
                continue
            if record.action_type == "navigate":
                if str(record.params.get("method", "GET")).upper() != "GET":
                    raise ConfigError(f"step {record.step}: only GET navigation is replayable")
                if record.params.get("headers") or record.params.get("body"):
                    raise ConfigError(
                        f"step {record.step}: request headers/body are not replayable"
                    )
                url = _redact_url(str(record.params.get("url", "")), secret_values)
                response_data = record.params.get("response")
                if not isinstance(response_data, dict):
                    raise ConfigError(f"step {record.step}: navigate response must be a mapping")
                response_url = _redact_url(str(response_data.get("url", url)), secret_values)
                if not url or response_url != url:
                    raise ConfigError(f"step {record.step}: navigate and response URLs must match")
                if url in seen_urls:
                    raise ConfigError(f"step {record.step}: duplicate cached URL {url!r}")
                seen_urls.add(url)
                body, status = _sanitize_response(response_data, url, secret_values)
                plans.append({"step": record.step, "url": url, "extract": []})
                responses.append(
                    (
                        url,
                        body,
                        status,
                        _sanitize_headers(response_data.get("headers"), secret_values),
                    )
                )
                current_step = record.step
                current_url = url
                continue
            if record.action_type != "extract":
                raise ConfigError(
                    f"step {record.step}: action {record.action_type!r} is not statically replayable"
                )
            if (
                current_step < 0
                or int(record.params.get("source_step", current_step)) != current_step
            ):
                raise ConfigError(
                    f"step {record.step}: extract needs a preceding navigate snapshot"
                )
            selector = _redact_text(str(record.params.get("selector", "")), secret_values)
            fields = record.params.get("fields")
            if not selector or not isinstance(fields, dict) or not fields:
                raise ConfigError(f"step {record.step}: extract needs selector and fields")
            try:
                root = Response(current_url, 200, responses[-1][1]).selector.css(selector)
            except Exception as exc:
                raise ConfigError(f"step {record.step}: invalid selector {selector!r}") from exc
            if len(root) != 1 or not hasattr(root[0], "element"):
                raise ConfigError(
                    f"step {record.step}: selector must match exactly one recorded root"
                )
            root_element = root[0].element
            compiled_fields: dict[str, dict[str, Any]] = {}
            for name, spec_value in fields.items():
                if not isinstance(name, str) or not isinstance(spec_value, dict):
                    raise ConfigError(f"step {record.step}: invalid field specification")
                spec = dict(spec_value)
                field_selector = _redact_text(str(spec.get("selector", "")), secret_values)
                matched = (
                    root_element.cssselect(field_selector) if field_selector else [root_element]
                )
                if len(matched) != 1:
                    raise ConfigError(
                        f"step {record.step}: field {name!r} needs one recorded match"
                    )
                expected = spec.get("type", "str")
                if expected not in _TYPE_MAP:
                    raise ConfigError(f"step {record.step}: unsupported field type {expected!r}")
                compiled_fields[name] = {
                    "selector": field_selector,
                    "attribute": _redact_text(str(spec.get("attribute", "text")), secret_values),
                    "type": expected,
                    "required": bool(spec.get("required", True)),
                    **(
                        {"default": _redact_text(str(spec["default"]), secret_values)}
                        if "default" in spec and isinstance(spec["default"], str)
                        else {"default": spec["default"]}
                        if "default" in spec
                        else {}
                    ),
                    "fingerprint": _fingerprint(matched[0]),
                }
            plans[-1]["extract"].append(
                {
                    "step": record.step,
                    "selector": selector,
                    "root_fingerprint": _fingerprint(root_element),
                    "fields": compiled_fields,
                    "confidence": _probability(record.params.get("confidence", 1.0)),
                    "min_confidence": _probability(record.params.get("min_confidence", 0.8)),
                    "adaptive_threshold": _probability(
                        record.params.get("adaptive_threshold", 0.5)
                    ),
                }
            )

        if not any(plan["extract"] for plan in plans):
            raise ConfigError("no statically replayable extraction steps")
        target.mkdir(parents=True, exist_ok=True)
        cache = CacheMiddleware(target / "cache")
        for url, body, status, headers in responses:
            request = Request(url)
            response = Response(url, status, body, headers=headers)
            cache.process_response(response, request, Spider())
        (target / "replay.json").write_text(
            json.dumps({"version": 1, "plans": plans}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (target / "spider.py").write_text(_SPIDER_TEMPLATE, encoding="utf-8")
        return target


def compile_static_replay(
    records: list[ActionRecord],
    output_dir: str | Path,
    *,
    secrets: list[str] | tuple[str, ...] = (),
) -> Path:
    """Compile records into an offline Spider project directory."""
    return StaticReplayCompiler().compile(records, output_dir, secrets=secrets)


def _probability(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"confidence must be numeric, got {value!r}") from exc
    if not 0.0 <= score <= 1.0:
        raise ConfigError(f"confidence must be between 0 and 1, got {score}")
    return score


def _fingerprint(element: Any) -> str:
    from ..parser.adaptive import compute_fingerprint

    return compute_fingerprint(element)


def _field_value(element: Any, attribute: str) -> str:
    if attribute != "text":
        return str(element.get(attribute, ""))
    return " ".join("".join(element.itertext()).split())


def extract_static_items(response: Response, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract records with deterministic CSS then fingerprint fallback."""
    from lxml import etree

    candidates = [
        item.element
        for item in response.selector.css("*")
        if isinstance(item.element, etree._Element)
    ]
    root_matches = [item.element for item in response.selector.css(plan["selector"])]
    roots = root_matches
    root_score = 1.0
    if not roots:
        match, root_score = _best_match(
            candidates, plan["root_fingerprint"], plan["adaptive_threshold"]
        )
        roots = [match] if match is not None else []
    output: list[dict[str, Any]] = []
    for root in roots:
        values: dict[str, Any] = {}
        confidence = min(1.0, root_score, float(plan["confidence"]))
        for name, spec in plan["fields"].items():
            selector = spec["selector"]
            matches = root.cssselect(selector) if selector else [root]
            score = 1.0
            if not matches and selector:
                match, score = _best_match(
                    list(root.iter()), spec["fingerprint"], plan["adaptive_threshold"]
                )
                matches = [match] if match is not None else []
            if matches:
                confidence = min(confidence, score)
                values[name] = _field_value(matches[0], spec["attribute"])
        output.append(
            {
                "url": response.url,
                **values,
                "_confidence": confidence,
                "_min_confidence": float(plan["min_confidence"]),
            }
        )
    if not roots:
        output.append(
            {"url": response.url, "_confidence": 0.0, "_min_confidence": plan["min_confidence"]}
        )
    return output


def _best_match(candidates: list[Any], fingerprint: str, threshold: float) -> tuple[Any, float]:
    from ..parser.adaptive import best_match

    return best_match(candidates, fingerprint, threshold=threshold)


class ContractPipeline:
    """Quarantine missing/type/low-confidence items before feed export."""

    def __init__(self, quarantine_output: str | Path) -> None:
        self.quarantine_output = Path(quarantine_output)

    def process_item(self, item: Any, spider: Spider) -> Any:
        from ..spider.spider import DropItem as Drop

        if not isinstance(item, dict):
            self._quarantine(item, ["item_type"])
            raise Drop("item contract: expected mapping")
        confidence = item.get("_confidence", -1.0)
        minimum = item.get("_min_confidence", 0.8)
        if not isinstance(confidence, (int, float)) or confidence < minimum:
            self._quarantine(item, ["low_confidence"])
            raise Drop("item quarantined: low confidence")
        validators = {
            name: _validator_for(name, spec) for name, spec in item.pop("_field_specs", {}).items()
        }
        del item["_min_confidence"]
        coerced = dict(item)
        for name, spec in validators.items():
            validator, kind = spec
            if name in coerced and kind in {"int", "float", "bool"}:
                try:
                    coerced[name] = _coerce(coerced[name], kind)
                except (TypeError, ValueError):
                    pass
        try:
            for name, (validator, _kind) in validators.items():
                single = {name: coerced[name]} if name in coerced else {}
                converted = validator.process_item(single, spider)
                if name in converted:
                    coerced[name] = converted[name]
            return coerced
        except Drop as exc:
            self._quarantine(coerced, [str(exc)])
            raise

    def _quarantine(self, item: Any, reasons: list[str]) -> None:
        self.quarantine_output.parent.mkdir(parents=True, exist_ok=True)
        with self.quarantine_output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"reasons": reasons, "item": item}, ensure_ascii=False) + "\n")


def _validator_for(name: str, spec: dict[str, Any]) -> tuple[ValidationPipeline, str]:
    kind = str(spec.get("type", "str"))
    constraint: dict[str, Any] = {
        "type": _TYPE_MAP[kind],
        "required": bool(spec.get("required", True)),
    }
    if "default" in spec:
        constraint["default"] = spec["default"]
    return ValidationPipeline({name: constraint}), kind


def _coerce(value: Any, kind: str) -> Any:
    if kind == "int" and not isinstance(value, bool):
        return int(value)
    if kind == "float" and not isinstance(value, bool):
        return float(value)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in {"true", "1"}:
            return True
        if str(value).lower() in {"false", "0"}:
            return False
        raise ValueError("not boolean")
    return value


class IdempotentFeedPipeline:
    """Deduplicate against the existing JSONL feed before appending."""

    def __init__(self, output: str | Path) -> None:
        self.output = Path(output)
        self.feed = FeedExportPipeline(output)
        self.seen: set[str] = set()
        if self.output.exists():
            for line in self.output.read_text(encoding="utf-8").splitlines():
                if line:
                    record = json.loads(line)
                    self.seen.add(str(record["_dedup_key"]))

    def process_item(self, item: Any, spider: Spider) -> Any:
        if not isinstance(item, dict):
            return item
        key = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if key in self.seen:
            from ..spider.spider import DropItem as Drop

            raise Drop("duplicate replay item")
        item = {**item, "_dedup_key": key}
        result = self.feed.process_item(item, spider)
        self.seen.add(key)
        return result


_SPIDER_TEMPLATE = '''"""Generated offline static replay Spider."""
import json
from pathlib import Path

from web_crawler.spider import CacheMiddleware, Request, Spider
from web_crawler.spider.static_replay import ContractPipeline, IdempotentFeedPipeline, extract_static_items

BASE = Path(__file__).resolve().parent


class OfflineOnlyFetcher:
    def get(self, url, **kwargs):
        raise AssertionError(f"offline replay attempted network fetch: {url}")


class CompiledReplaySpider(Spider):
    name = "compiled_static_replay"
    middlewares = [CacheMiddleware(BASE / "cache", offline=True)]
    item_pipelines = [ContractPipeline(BASE / "quarantine.jsonl"), IdempotentFeedPipeline(BASE / "items.jsonl")]

    def __init__(self):
        super().__init__(fetcher=OfflineOnlyFetcher())

    def start_requests(self):
        for plan in self.plan_groups:
            yield Request(plan["url"], meta={"plans": plan["extract"]})

    def parse(self, response):
        for plan in response.meta["plans"]:
            for item in extract_static_items(response, plan):
                item["_field_specs"] = plan["fields"]
                yield item


def main():
    spider = CompiledReplaySpider()
    spider.plan_groups = json.loads((BASE / "replay.json").read_text(encoding="utf-8"))["plans"]
    items = spider.run()
    print(json.dumps({"items": items}, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''


__all__ += ["ContractPipeline", "IdempotentFeedPipeline", "extract_static_items"]
