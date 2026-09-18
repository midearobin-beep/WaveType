"""LLM 润色：Ollama 本地小模型，think 关闭，速度优先。

职责（对应 Typeless L3 重写层的最小集）：
- 去填充词（嗯、那个、就是说……）
- 识别中途改口，只保留最终意图（"周三——不对，周四" → "周四"）
- 补标点、适度分段；中英混排保持原样
- 不改写观点、不扩写、不翻译
"""
from __future__ import annotations

import logging
import os

# 必须在 import ollama 之前执行：ollama 包在 import 时就实例化全局 httpx Client，
# 一旦环境里存在 SOCKS 代理变量（Veee 的 all_proxy），即使目标被 NO_PROXY 豁免，
# httpx 构建 proxy transport 时也会因缺 socksio 直接 ImportError。
# 本应用全部流量都是本机（Ollama），直接清除代理变量最稳妥。
for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)

import ollama

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你把语音识别的口语原文整理成用户想打出的文字。

要做：
- 去掉口癖填充词（嗯、呃、那个、就是说等），口吃重复只留一遍
- 中途改口只保留最终说法（"周三不对周四" → 周四）
- 把破碎口语重组为通顺的书面句：允许调整语序、合并碎句、按语义分段
- 补齐标点；中英混排各保持原语言
- 明确列举多项时整理成纯文本编号列表（1. 2. 3.，换行，子项 Tab 缩进），
  单事项保持段落。禁用 Markdown 符号（-、*、#、**、>）

不做：
- 不加原文没有的信息，不改变观点和事实，不翻译
- 只输出整理后的正文，无解释、无引号、无前后缀

示例一：
输入：嗯那个我想说明天的会改到周四下午三点然后呢记得带上测试报告
输出：明天的会改到周四下午三点，记得带上测试报告。

示例二：
输入：报销流程第一步先填那个电子表单然后在OA里面提交嗯提交完等领导审批就行了
输出：报销流程：
1. 填写电子表单
2. 在 OA 里提交
3. 等领导审批
"""

DICT_TEMPLATE = "\n用户个人词典（以下写法必须严格遵守，左边是常被误识别的形式）：\n%s\n"


class Polisher:
    def __init__(self, model: str = "qwen3.5:4b-mlx", base_url: str = "http://127.0.0.1:11434",
                 think: bool = False, enabled: bool = True) -> None:
        # trust_env=False：忽略 Veee 等系统代理环境变量（本地服务必须直连，
        # 否则 httpx 会尝试走 SOCKS 代理而报 socksio 缺失）
        self._client = ollama.Client(host=base_url, trust_env=False)
        self._model = model
        self._think = think
        self.enabled = enabled
        self._mappings: list[tuple[str, str]] = []

    def set_dictionary(self, mappings: list[tuple[str, str]]) -> None:
        """注入个人词典纠错映射 [(错误形式, 正确形式)]。"""
        self._mappings = mappings

    def polish(self, text: str) -> str:
        if not self.enabled or not text.strip():
            return text
        system = SYSTEM_PROMPT
        if self._mappings:
            pairs = "\n".join(f"{w} → {r}" for w, r in self._mappings)
            system += DICT_TEMPLATE % pairs
        resp = self._client.chat(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            think=self._think,
            options={"temperature": 0.1},
        )
        out = (resp.message.content or "").strip()
        log.info("润色: %s → %s", text, out)
        # 兜底：小模型偶发丢内容/复读/膨胀，异常则回退原文。
        # 长度阈值分档：长段口语本来就该大幅压缩（去口癖+重组），
        # 固定 0.5 下限会把正常的长文本清洗误判为"丢内容"而回退原文。
        n = len(text)
        min_ratio = 0.5 if n < 100 else (0.35 if n < 300 else 0.25)
        if not out or len(out) < n * min_ratio or len(out) > n * 1.6:
            log.warning("润色结果异常（长度 %d→%d，阈值 %.2f），回退 ASR 原文",
                        n, len(out), min_ratio)
            return text
        if _has_repetition_loop(out):
            log.warning("润色结果异常（复读），回退 ASR 原文")
            return text
        return out


def _has_repetition_loop(text: str) -> bool:
    """检测复读机式输出：某片段（2–12 字）连续重复 3 次以上。"""
    for size in (2, 3, 4, 6, 8, 12):
        for i in range(0, len(text) - size * 3):
            seg = text[i:i + size]
            if text[i + size:i + size * 2] == seg and text[i + size * 2:i + size * 3] == seg:
                return True
    return False
