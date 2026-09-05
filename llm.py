"""大模型调用层：OpenAI 兼容 chat/completions 接口。

call_model()：通用文本生成（总结、报告）。
RetriableModelError：超时/网络/5xx，调用方可重试；4xx 与解析错误抛普通 RuntimeError 不重试。
"""

import json
import os
import socket
import urllib.error
import urllib.request


class RetriableModelError(RuntimeError):
    """可重试的模型调用失败（超时/网络/HTTP 5xx）。"""


def _api_url():
    api_url = os.environ.get("MODEL_API_URL", "").strip()
    if not api_url:
        raise RuntimeError("请先在 .env 里填写 MODEL_API_URL")
    if api_url.rstrip("/").endswith("/v1"):
        api_url = api_url.rstrip("/") + "/chat/completions"
    return api_url


def call_model(prompt, system=None, max_tokens=None, temperature=0.2):
    """调用大模型生成文本，返回纯文本内容。

    system: 默认客服助手提示词；max_tokens: 覆盖 MODEL_MAX_TOKENS。
    """
    api_url = _api_url()
    api_key = os.environ.get("MODEL_API_KEY", "").strip()
    model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini").strip()
    timeout_seconds = int(os.environ.get("MODEL_TIMEOUT_SECONDS", "180") or "180")
    if max_tokens is None:
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", "8000") or "8000")

    if not api_key:
        raise RuntimeError("请先在 .env 里填写 MODEL_API_KEY")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        # 关掉推理模式：deepseek-v4-pro 等推理模型思考 token 是超时的根因
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message = f"模型接口请求失败：HTTP {exc.code} {detail}"
        if exc.code >= 500:
            raise RetriableModelError(message) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RetriableModelError(
                f"模型接口超时（超过 {timeout_seconds}s）：请调大 MODEL_TIMEOUT_SECONDS 或减少单批条数"
            ) from exc
        raise RetriableModelError(f"模型接口网络错误：{reason}") from exc

    try:
        content = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"无法解析模型返回：{json.dumps(result, ensure_ascii=False)}") from exc

    if not content:
        raise RuntimeError(
            "模型返回内容为空（可能被 max_tokens 截断或推理未完成），请调大 max_tokens 后重试"
        )
    return content


def _stream_request(messages, system=None, max_tokens=None, temperature=0.2):
    """构建流式请求并返回打开的响应对象（调用方负责 close）。"""
    api_url = _api_url()
    api_key = os.environ.get("MODEL_API_KEY", "").strip()
    model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini").strip()
    if max_tokens is None:
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", "8000") or "8000")

    if not api_key:
        raise RuntimeError("请先在 .env 里填写 MODEL_API_KEY")

    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)

    payload = {
        "model": model_name,
        "messages": payload_messages,
        "temperature": temperature,
        # 关掉推理模式：deepseek-v4-pro 等推理模型思考 token 是超时的根因
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
        "stream": True,
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    timeout_seconds = int(os.environ.get("MODEL_TIMEOUT_SECONDS", "180") or "180")
    return urllib.request.urlopen(request, timeout=timeout_seconds)


def call_model_stream(messages, system=None, max_tokens=None, temperature=0.2):
    """流式调用大模型，逐段 yield 文本增量。

    messages: [{"role","content"}, ...] 多轮消息列表（system 单独传入并置于最前）。
    错误语义与 call_model 一致：连接失败（5xx/超时/网络）抛 RetriableModelError，
    4xx 与解析错误抛 RuntimeError；首段输出后仍可能抛异常，调用方需自行收尾。
    """
    try:
        response = _stream_request(messages, system=system, max_tokens=max_tokens, temperature=temperature)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message = f"模型接口请求失败：HTTP {exc.code} {detail}"
        if exc.code >= 500:
            raise RetriableModelError(message) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RetriableModelError(
                f"模型接口超时（超过 {os.environ.get('MODEL_TIMEOUT_SECONDS', '180')}s）：请调大 MODEL_TIMEOUT_SECONDS"
            ) from exc
        raise RetriableModelError(f"模型接口网络错误：{reason}") from exc

    try:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise RuntimeError(f"无法解析流式返回：{data[:200]}") from exc
            text = delta.get("content") or ""
            if text:
                yield text
    finally:
        response.close()
