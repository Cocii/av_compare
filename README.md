# AV Compare — 音视频文件夹对比工具

一个单文件 Web 应用，用于在浏览器中并排对比多个文件夹中的音频/视频文件，支持评分、备注、收藏集管理等功能。

## 快速启动

```bash
python app.py [--port 8765] [--host 0.0.0.0] [--db my_database.db]
```

启动后打开浏览器访问 `http://localhost:8765`。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | 8765 | 服务端口 |
| `--host` | 0.0.0.0 | 监听地址 |
| `--db` | my_database.db | SQLite 数据库路径 |

## 技术栈

- **后端**: FastAPI + Uvicorn + SQLite
- **前端**: 内嵌 HTML/CSS/JS（单文件，无构建步骤）
- **依赖**: `fastapi`, `uvicorn`

```bash
pip install fastapi uvicorn
```

## 功能概览

### 1. 文件夹管理

- **添加文件夹**: 输入服务器上的目录路径，自动扫描其中的音视频文件（支持 `.wav`, `.mp3`, `.flac`, `.mp4`, `.avi`, `.mkv` 等格式）
- **自动检测类型**: 根据文件扩展名自动识别音频/视频类型
- **编辑/删除**: 支持修改名称、路径、描述，删除仅清除注册记录，不删除实际文件
- **搜索过滤**: 按名称、路径、描述搜索，按音频/视频类型过滤
- **智能 DB 路径**: 在 JuiceFS/NFS 等网络文件系统上自动 fallback 到 `/tmp`，避免文件锁问题

### 2. 对比视图（Compare）

核心功能，支持多文件夹并排对比：

- **多面板对比**: 同时选择多个文件夹，并排显示文件列表
- **文件级评分**: 对每个文件进行 1-5 星评分
- **备注**: 为每个文件添加文字备注
- **播放同步**: 滚动同步，方便逐文件对比
- **文本展示**: 自动加载文件夹中的 `name2text.json`，显示每个文件对应的文本（如 ASR 结果）
- **面板拖拽排序**: 可拖动调整对比面板顺序
- **面板宽度可调**: 拖拽面板边缘调整宽度
- **只读模式**: URL 加 `?readonly=1` 参数可锁定编辑功能
- **URL 持久化**: 对比选择通过 URL 参数保存，刷新页面不丢失

### 3. 收藏集（Collections）

- **保存对比状态**: 将当前对比的文件夹组合保存为 Collection
- **加载历史对比**: 随时切换回之前保存的 Collection
- **管理**: 支持创建、编辑、删除收藏集

### 4. 数据持久化

工具支持两种数据源，自动合并：

- **SQLite 数据库**: 运行时实时写入
- **文件夹本地 JSON**:
  - `name2text.json`: 文件名到文本的映射（如 ASR 转录结果）
  - `name2json.json`: 文件名到评分/备注/上下文等元数据的映射

加载文件夹时，自动导入已有的 `name2json.json` 数据到 SQLite；评分变更时，同步回写到 `name2json.json`。

## API 接口

### Folders

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/api/folders` | 注册新文件夹 |
| `GET` | `/api/folders` | 列出所有文件夹 |
| `GET` | `/api/folders/{id}/files` | 获取文件夹内文件列表、文本、评分 |
| `PUT` | `/api/folders/{id}` | 更新文件夹信息 |
| `DELETE` | `/api/folders/{id}` | 删除文件夹注册 |

### Ratings

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/api/ratings` | 设置/更新文件评分和备注 |
| `GET` | `/api/ratings/{folder_id}` | 获取文件夹所有评分 |

### Collections

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/api/collections` | 创建收藏集 |
| `GET` | `/api/collections` | 列出所有收藏集 |
| `GET` | `/api/collections/{id}` | 获取收藏集详情 |
| `PUT` | `/api/collections/{id}` | 更新收藏集 |
| `DELETE` | `/api/collections/{id}` | 删除收藏集 |

### 文件服务

| Method | Path | 说明 |
|--------|------|------|
| `GET` | `/files/{file_path}` | 流式传输音视频文件，支持 HTTP Range 请求（断点续播） |

## 支持的媒体格式

**音频**: `.wav`, `.mp3`, `.flac`, `.aac`, `.ogg`, `.m4a`, `.wma`, `.opus`

**视频**: `.mp4`, `.avi`, `.mkv`, `.webm`, `.mov`, `.flv`, `.wmv`, `.m4v`

## 项目结构

```
av_compare/
├── app.py                      # 主应用（后端 + 前端）
└── my_database.db              # SQLite 数据库（自动生成）
```

## 安全说明

- 文件服务接口 (`/files/`) 会对路径进行校验，仅允许访问已注册的文件夹下的文件
- 删除文件夹仅清除数据库记录，不会删除磁盘上的实际文件
