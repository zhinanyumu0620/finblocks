"""实际 DeepSeek 调用；只读取环境密钥，不保存凭据或思考内容。"""

import hashlib
from datetime import datetime, timezone
import json
import os
import time
import urllib.error
import urllib.request

from .dsl import compile_strategy
from .research import audit_intent


class AIError(ValueError):
    """真实 AI 不可用或输出无法验证。"""


EXAMPLE = {
    "version": 1, "name": "MA 条件示例",
    "nodes": [{"id": "price", "op": "field", "field": "close"},
              {"id": "fast", "op": "ma", "input": "price", "window": 5},
              {"id": "slow", "op": "ma", "input": "price", "window": 20},
              {"id": "entry", "op": "gt", "left": "fast", "right": "slow"}],
    "signal": "entry", "allocation": {"when_true": 1.0, "when_false": 0.0},
}
SYSTEM = """你是 FinBlocks 策略结构生成器。仅返回一个 JSON 对象，不输出代码或投资收益。
策略根结构必须且仅包含 version=1、name、nodes、signal、allocation。
节点只允许：field(id,op,field)，字段为 open/high/low/close/volume；
ma/ema/rsi(id,op,input,window)，窗口整数2至500；const(id,op,value)，有限数值；
bollinger(id,op,input,window,multiplier,band)，窗口2至500，0<multiplier<=10，band为upper/middle/lower；
macd(id,op,input,fast,slow,signal,component)，整数2<=fast<slow<=500、2<=signal<=500，component为line/signal/histogram；
MACD的line为DIF，signal为DEA，histogram=DIF-DEA不乘2；EMA用窗口SMA播种，RSI用Wilder平滑。
gt/lt/cross_up/cross_down(id,op,left,right)，数值输入；
and/or(id,op,left,right)，布尔输入。节点id唯一，不得循环或有未使用节点。
signal引用布尔节点；allocation仅when_true/when_false，数值为0至1。
gt表示持续高于，cross_up表示单次向上穿越；不得混淆。
当前没有已核实的基本面PIT、指数、复权切换、分钟数据，不生成相关策略或假字段。
需求明确且可支持时返回 {"status":"ok","strategy":策略,"notes":[中文说明]}。
不支持、含任意代码、要求承诺收益或参数不明确时返回
{"status":"unsupported","reason":"具体原因"}。所有参数来自用户需求，不擅自补参数。
实际执行时点和费用由独立回测配置决定，策略不得设置同日成交或未来数据。
JSON 格式例子（这些窗口是格式示例，不是默认用户参数）：
""" + json.dumps({"status": "ok", "strategy": EXAMPLE, "notes": []}, ensure_ascii=False)


def _request(path, payload=None, timeout=40):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key or not key.strip():
        raise AIError("缺少 DEEPSEEK_API_KEY；不会返回假模型结果")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request("https://api.deepseek.com" + path, data=encoded,
                                    headers={"Authorization": "Bearer " + key.strip(),
                                             "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise AIError("模型响应超过项目读取上限")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # 不输出服务响应正文，避免错误页回显请求或凭据。
        raise AIError(f"DeepSeek HTTP {exc.code}；请检查网络、账号权限或额度") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AIError("DeepSeek 网络不可达或超时；不会回退为假 AI") from None
    except (ValueError, UnicodeError):
        raise AIError("DeepSeek 响应不是有效 JSON") from None


def list_models():
    response = _request("/models")
    return [item["id"] for item in response.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]


def generate_strategy(prompt: str, model="deepseek-flash"):
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
        raise AIError("策略需求须为 1–4000 个字符")
    if model not in list_models():
        raise AIError("所选模型不在实际 API 返回的可用模型列表中")
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
               "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "max_tokens": 1800}
    started = time.perf_counter()
    response = _request("/chat/completions", payload)
    try:
        choice = response["choices"][0]
        content = choice["message"]["content"]
        if choice["finish_reason"] != "stop" or not isinstance(content, str) or not content.strip():
            raise AIError("模型输出为空、不完整或被截断，未执行")
        def reject_nonfinite(value):
            raise ValueError("JSON 非有限常量")
        parsed = json.loads(content, parse_constant=reject_nonfinite)
        if not isinstance(parsed, dict):
            raise AIError("模型响应根结构须为 JSON 对象")
        evidence = {"provider": "DeepSeek", "model_requested": model, "model_returned": response.get("model"),
                    "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "server_created": response.get("created"),
                    "response_id": response.get("id"), "finish_reason": choice["finish_reason"],
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "usage": response.get("usage"), "prompt": prompt,
                    "system_prompt_sha256": hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest(),
                    "response": parsed, "generated_by_real_api": True,
                    "data_sent": "用户策略需求和DSL格式；没有发送本地行情或财务包"}
        if parsed.get("status") == "unsupported":
            return {"status": "unsupported", "reason": parsed.get("reason", "模型拒绝该需求"), "evidence": evidence}
        if parsed.get("status") != "ok" or "strategy" not in parsed:
            raise AIError("模型返回未知状态或缺少策略")
        compiled = compile_strategy(parsed["strategy"])
        return {"status": "ok", "strategy": parsed["strategy"], "warmup_records": compiled.warmup, "evidence": evidence,
                "intent_audit": audit_intent(prompt, parsed["strategy"])}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        if isinstance(exc, AIError):
            raise
        raise AIError("模型输出未通过 JSON/DSL 校验，未执行；" + str(exc)) from None


def research_call(system, document, model="deepseek-flash"):
    """研究请求只传明确确认的数据摘要，不保存凭据或模型思考。"""
    system = system + "\n严格只输出json对象。"  # 服务JSON模式要求提示中出现json。
    if model not in list_models():
        raise AIError("所选模型不在实际API可用列表中")
    payload = {"model": model, "messages": [{"role": "system", "content": system},
               {"role": "user", "content": json.dumps(document, ensure_ascii=False, allow_nan=False)}],
               "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "max_tokens": 1800}
    started = time.perf_counter()
    response = _request("/chat/completions", payload)
    try:
        choice = response["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ValueError("输出不完整")
        def invalid_constant(_value):
            raise ValueError("非有限JSON常量")
        parsed = json.loads(choice["message"]["content"], parse_constant=invalid_constant)
        if not isinstance(parsed, dict):
            raise ValueError("根结构不是对象")
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIError("研究模型响应未通过结构校验，未执行") from None
    return parsed, {"provider": "DeepSeek", "model_returned": response.get("model"), "response_id": response.get("id"),
                    "model_requested": model, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                    "usage": response.get("usage"), "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "generated_by_real_api": True, "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
                    "data_sent": "确认的研究问题或选定指标/账目摘要；未发送原行情包、凭据或因子最终测试结果"}


def explain_research(pack):
    system = """你是量化研究解释助手。只组织给定证据，不发明数字、新闻、因果或收益保证。
只返回 {"claims":[{"evidence_id":"给定事实id","explanation":"中文定性解释，不包含数字"}]}。
每条只解释被引用的事实。不得包含阿拉伯或中文数字、计数、具体日期；这些数值由界面直接展示原证据。
不要使用“接近零”“下一交易日”等额外数值表述，用“按设置的滞后执行”等定性表述。
明确历史关联不证明因果、未发现异常不证明没有过拟合。
不添加论文或任何新证据，最多8条。"""
    parsed, evidence = research_call(system, pack)
    facts = {item["id"]: item for item in pack["facts"]}
    claims = parsed.get("claims")
    if set(parsed) != {"claims"} or not isinstance(claims, list) or not 1 <= len(claims) <= 8:
        raise AIError("解释结构不符合证据引用要求")
    import re
    for claim in claims:
        if (not isinstance(claim, dict) or set(claim) != {"evidence_id", "explanation"}
                or not isinstance(claim["evidence_id"], str) or claim["evidence_id"] not in facts or not isinstance(claim["explanation"], str)
                or not 1 <= len(claim["explanation"]) <= 500
                or re.search(r"[0-9]|[零〇一二三四五六七八九十百千万亿两]{2,}|[零〇一二三四五六七八九十百千万亿两](?:条|期|笔|个|倍|元|天|年|交易日)|百分之", claim["explanation"])):
            raise AIError("AI解释包含无效引用、超长文本或自行陈述数字；保留确定性证据，不应用解释")
    return {"claims": claims, "evidence": evidence,
            "validation_scope": "已校验结构、引用存在及阿拉伯/部分中文数量表达；不等于完整数值或语义证明，解释仍需人工核读"}


def generate_factor(prompt):
    from .factors import compile_factor
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
        raise AIError("因子研究问题须为1–4000字")
    system = """你是受约束因子候选助手。只返回JSON，不写Python代码，不承诺收益，不计算绩效。
支持字段open/high/low/close/volume，有限常量，加减乘除，MA/EMA/RSI/STD/LAG(x,整数窗口)。
MA/EMA/RSI/STD窗口2–500，LAG滞后1–500。禁止未来字段、财务估值、任意属性或函数。
所有窗口和参数必须来自用户明确需求，不自行补默认值。缺参数或能力不足时返回
{"status":"unsupported","reason":"具体原因"}。需求可支持时返回
{"status":"ok","expression":"受约束算式","hypothesis":"中文经济假设，尚未证实","failure_conditions":"可能失效的条件"}。
生成一个候选，不搜索最赚钱参数，不编造论文。"""
    parsed, evidence = research_call(system, {"question": prompt})
    if parsed.get("status") == "unsupported" and isinstance(parsed.get("reason"), str):
        return {"status": "unsupported", "reason": parsed["reason"], "evidence": evidence}
    if (set(parsed) != {"status", "expression", "hypothesis", "failure_conditions"} or parsed["status"] != "ok"
            or any(not isinstance(parsed[k], str) or not 1 <= len(parsed[k]) <= 1500 for k in ("expression", "hypothesis", "failure_conditions"))):
        raise AIError("因子候选结构不合法")
    compiled, _, _ = compile_factor(parsed["expression"])
    import re
    explicit_numbers = {int(n) for n in re.findall(r"(?<![0-9])[0-9]+(?![0-9])", prompt)}
    if any(n["window"] not in explicit_numbers for n in compiled.ordered_nodes if "window" in n):
        raise AIError("模型引入了需求中没有明确给出的窗口，候选未应用；请补充参数")
    from .factors import describe_factor
    return {**parsed, "evidence": evidence, "description": describe_factor(compiled, parsed["expression"]),
            "scope": "AI假设尚未验证；表达式经过白名单检查，参数与意图仍须人工确认"}
