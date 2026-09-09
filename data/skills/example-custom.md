# 自定义 Skill 示例

将以下 YAML 保存到 `data/skills/your_skill.yaml` 即可自动加载。

## 示例：翻译 skill

```yaml
name: translator
description: "中英互译专家，只做翻译，保留原意与语气。"
type: prompt
prompt: |
  你是一个专业翻译。只做翻译，不做其他。
  保留原文语气、术语与格式。
parameters:
  - name: text
    type: string
    description: "要翻译的文本"
  - name: target_lang
    type: string
    description: "目标语言，如 en 或 zh"
permission: public
```

## 示例：摘要 skill

```yaml
name: summarizer
description: "长文本摘要专家，输出 concise bullet summary。"
type: prompt
prompt: |
  你是一个摘要专家。将输入文本压缩为简洁的要点摘要，
  保留关键数字与结论。
parameters:
  - name: text
    type: string
    description: "要摘要的文本"
  - name: max_bullets
    type: integer
    description: "最大要点数，默认 5"
permission: public
```
