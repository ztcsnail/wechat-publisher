# 输入与版式

任务目录示例：

```text
/tmp/wechat-publisher/20261009-topic/
  article.json
  article.md
  图片/cover.png
  图片/inline-1.png
  图片/inline-2.png
```

`article.json`：

```json
{
  "title": "文章标题",
  "author": "",
  "digest": "120 字以内的摘要",
  "markdown": "article.md",
  "cover": "图片/cover.png",
  "theme": "editorial",
  "content_source_url": "",
  "need_open_comment": 1,
  "only_fans_can_comment": 0,
  "request_original_declaration": true,
  "collection": "成长"
}
```

标题最多 64 字，作者最多 16 字。实际微信账号接口限制以微信返回为准。默认开放留言，所有读者可留言；通过草稿读取核验设置。`content_source_url` 可留空；非空必须是 http/https。`request_original_declaration` 和 `collection` 是交付待办元数据，不作为微信接口字段发送；原创声明与合集需要在编辑器设置。

正文用段落、`##`/`###` 标题、强调、列表、引用、链接或简单表格；插图示例 `![图注](图片/inline-1.png)`。不要塞入外部样式、iframe、脚本、原始复杂 HTML 或外部图片 URL。本地图片只能引用任务目录中的 PNG/JPEG 文件，原图保存，上传时编码为 JPEG；透明像素用白底。封面和方形封面由微信裁剪坐标适配。

正文图通过 `media/uploadimg` 返回的 URL 插入，封面通过永久素材接口得到 `thumb_media_id`。构建会在图片丢失、路径越界、过大正文或未经显式允许的无插图文章时停止。纯文字文章须设置 `allow_no_images: true`。

构建返回可离线打开的预览 HTML、微信公众号内联样式正文、源 Markdown、素材与校验清单。最终用户源输入另存 `原始输入/口述原文.txt`，不要覆盖用户文件。

运行环境已具备 Python、Markdown、Pillow。迁移机器时依赖声明为 `config/requirements.txt`；遵守目标机器安装权限。
