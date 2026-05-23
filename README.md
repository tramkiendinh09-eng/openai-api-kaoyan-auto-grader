# 基于 OpenAI API 打造的考研真题自动改卷

这是一个本地运行的考研数学自动阅卷原型。系统接收学生答卷 PDF、题目 PDF 和参考答案 PDF，结合 MinerU/OCR 文本、PDF 页图视觉识别和 OpenAI 兼容 API，对选择题、填空题和解答题进行分题阅卷，并输出每题得分、扣分原因、总分、复核标记和审计日志。

## 核心特性

- PDF/MinerU 解析：优先复用本地 MinerU Markdown，缺失时自动抽取 PDF 文本。
- 视觉 + OCR 复合阅卷：GPT 视觉读取卷面，MinerU OCR 只作为草稿交叉校对。
- 整卷视觉定位：先用一次 `xhigh` 视觉扫描定位每题作答顺序和区域，再并发分题阅卷。
- 分题型并发：选择题批量读取，填空题和解答题并发评分。
- 稳定 JSON 输出：每题结果、总报告和日志均可解析。
- 缓存与重试：支持模型响应缓存、单题结果缓存、流式接收和网络重试。
- 本地前端：无需部署，浏览器打开 `127.0.0.1:8765` 即可使用。

## 安装

```bash
pip install -r requirements.txt
```

还需要本机安装 Poppler，并确保 `pdftoppm` 在 PATH 中，用于把 PDF 渲染成页图。

## 启动本地前端

```bash
python scripts/auto_grade_web.py --host 127.0.0.1 --port 8765
```

然后打开：

```text
http://127.0.0.1:8765/
```

在页面中填写自己的 OpenAI 兼容 API 地址、模型名和 API Key。API Key 只进入本地子进程环境变量，不会写入项目文件。

## 命令行示例

```bash
set GRADER_API_KEY=你的_API_Key
python scripts/auto_grade_exam.py ^
  --submission-pdf "student.pdf" ^
  --question-paper-pdf "paper.pdf" ^
  --reference-pdf "answer.pdf" ^
  --api-url "https://your-gateway.example.com/v1" ^
  --model "gpt-5.5" ^
  --api-mode responses ^
  --questions 1-22 ^
  --layout-scan ^
  --objective-reasoning-effort high ^
  --blank-reasoning-effort high ^
  --solution-reasoning-effort high ^
  --concurrency 10 ^
  --blank-concurrency 3 ^
  --solution-concurrency 6
```

## 重要说明

本仓库不包含真题 PDF、学生答卷、参考答案、模型缓存、运行日志或 API Key。请把这些资料保存在本地，通过前端或命令行参数传入。

本项目是阅卷原型，不应直接替代人工复核。若卷面不清、参考答案不足、评分点需要推导或模型分歧较大，系统会标记 `needs_human_review=true`。
