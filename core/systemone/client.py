"""System One HTTP helpers shared by native model listing and decisions."""

import asyncio
import json
import math
from urllib.parse import urlsplit, urlunsplit

import aiohttp


class SystemOneError(Exception):
    """A safe local error, without response bodies or credentials."""

    def __init__(self, message, *, code="systemone_provider_failed"):
        super().__init__(message)
        self.code = code


def endpoint_url(base_url, path):
    try:
        base = urlsplit(str(base_url).strip())
        if (
            base.scheme not in {"http", "https"}
            or not base.hostname
            or base.username is not None
            or base.password is not None
            or base.query
            or base.fragment
        ):
            raise ValueError
        _ = base.port
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise SystemOneError(
            "请填写有效 http(s) 服务地址，不能含账号密码、查询参数或片段。"
        ) from None
    prefix = base.path.rstrip("/")
    for suffix in ("/v1/systemone", "/v1/decisions", "/v1/models"):
        if prefix.endswith(suffix):
            prefix = prefix[: -len(suffix)]
            break
    if prefix.endswith("/v1") and path.startswith("/v1/"):
        prefix = prefix[:-3]
    return urlunsplit((base.scheme, base.netloc, prefix + path, "", ""))


def parse_models(payload):
    raw = payload
    for _ in range(4):
        if not isinstance(raw, dict):
            break
        key = next(
            (key for key in ("models", "data", "list", "items") if key in raw), None
        )
        if key is None:
            break
        raw = raw[key]
    if not isinstance(raw, list):
        raise SystemOneError("列表接口未返回模型数组，请检查模型列表路径。")
    result, seen = [], set()
    for item in raw:
        model = item
        if isinstance(item, dict):
            model = next(
                (
                    item[key]
                    for key in ("id", "name", "model", "model_id")
                    if key in item
                ),
                None,
            )
        if (
            not isinstance(model, str)
            or not model
            or model.strip() != model
            or any(ord(char) < 32 for char in model)
        ):
            raise SystemOneError("模型列表含无效 ID。")
        if model not in seen:
            seen.add(model)
            result.append(model)
    return result


def validate_answers(envelope, questions):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("answers"), dict):
        raise SystemOneError("决策接口未返回有效 answers。")
    answers = {}
    for qid, spec in questions.items():
        item = envelope["answers"].get(qid)
        if not isinstance(item, dict) or item.get("type", spec["type"]) != spec["type"]:
            raise SystemOneError("决策答案缺失或类型不匹配。")
        kind = spec["type"]
        if kind == "noul":
            value = item.get("noul")
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise SystemOneError("noul 概率无效。")
            answers[qid] = {"type": "noul", "noul": value}
            continue
        confidence = item.get("confidence")
        probabilities = item.get("probabilities")
        if (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or not isinstance(probabilities, dict)
            or not probabilities
            or any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
                for value in probabilities.values()
            )
        ):
            raise SystemOneError("决策概率或置信度无效。")
        if kind == "choice":
            choice = item.get("choice")
            if not isinstance(choice, str) or choice not in spec["criteria"]:
                raise SystemOneError("choice 不在候选项中。")
            answers[qid] = {
                "type": kind,
                "choice": choice,
                "confidence": confidence,
                "probabilities": probabilities,
            }
        elif kind == "score":
            score = item.get("score")
            if (
                type(score) not in (int, float)
                or not math.isfinite(score)
                or not 0 <= score <= len(spec["criteria"]) - 1
            ):
                raise SystemOneError("score 超出范围。")
            answers[qid] = {
                "type": kind,
                "score": score,
                "confidence": confidence,
                "probabilities": probabilities,
            }
        else:
            raise SystemOneError("不支持该问题类型。")
    return answers


class SystemOneHTTPClient:
    """One lazy connection pool per native Provider, with no shared credentials."""

    def __init__(self):
        self._session = None
        self._closed = False

    def session(self):
        if self._closed:
            raise SystemOneError("System One 模型服务已关闭。", code="provider_closed")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        return self._session

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()


async def request_json(
    method, url, *, key, headers, timeout, payload=None, max_body=1024 * 1024, client=None
):
    parts = urlsplit(url)
    if (
        key
        and parts.scheme == "http"
        and parts.hostname not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise SystemOneError("携带密钥的远程服务需要 HTTPS。")
    request_headers = dict(headers)
    if key:
        request_headers["Authorization"] = "Bearer " + key
    request_headers.setdefault("Accept", "application/json")
    owned = client is None
    client = client or SystemOneHTTPClient()
    try:
        async with client.session().request(
            method,
            url,
            headers=request_headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise SystemOneError(
                    f"System One 请求失败（HTTP {response.status}）。", code=f"http_{response.status}"
                )
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(16384):
                size += len(chunk)
                if size > max_body:
                    raise SystemOneError("System One 响应过大。")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))
    except SystemOneError:
        raise
    except asyncio.TimeoutError:
        raise SystemOneError("System One 请求超时。", code="timeout") from None
    except aiohttp.ClientError:
        raise SystemOneError("System One 连接失败。", code="transport_error") from None
    except (ValueError, UnicodeError, RecursionError):
        raise SystemOneError("System One 未返回有效 JSON。", code="invalid_json") from None
    finally:
        if owned:
            await client.close()
