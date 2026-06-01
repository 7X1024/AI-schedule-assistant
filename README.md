# AI 日程助手

粘贴学校通知、群聊消息、邮件内容，AI 自动提取日程与待办事项。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
mkdir -p .streamlit
echo 'DEEPSEEK_API_KEY = "sk-your-key-here"' > .streamlit/secrets.toml

# 3. 启动
streamlit run app.py
```

## 部署到 Streamlit Cloud

1. 推送代码到 GitHub
2. 在 [Streamlit Cloud](https://streamlit.io/cloud) 关联仓库
3. 在 Settings → Secrets 中添加：
   ```
   DEEPSEEK_API_KEY = "sk-your-key-here"
   ```

## 项目结构

```
├── app.py           # Streamlit 主界面
├── models.py        # Pydantic 数据模型
├── ai_parser.py     # DeepSeek API 调用与解析
├── storage.py       # 本地 JSON 存储
├── data/            # 数据目录（自动创建）
│   ├── events.json
│   └── todos.json
└── requirements.txt
```

## 技术栈

- Streamlit · DeepSeek V4 Flash · Pydantic · Pandas
- 本地 JSON 文件存储

## Debug v1-2-4
增加了点击日程显示原文的功能，目前无bug

## Debug v1-3-2
修复了日程无法删除的bug，修复了上周的事件会显示在待定事件的bug