企业私有知识库前端（单页）

布局参考常见产品：Dify / FastGPT / MaxKB 一类
  - 左侧：品牌、新对话、历史会话、索引状态、重建/导入
  - 中间：多轮气泡、引用来源、空态推荐问题
  - 底部：输入框、附件、路由模式、发送

启动：
  uv run mykb serve
  浏览器打开 http://127.0.0.1:8000/

对接：
  POST /v1/chat （multipart）
  GET  /v1/health
  POST /v1/rag/rebuild
  POST /v1/sqlite/ingest

会话列表仅存在浏览器 localStorage；服务端记忆仍按 session_id 文件隔离。
