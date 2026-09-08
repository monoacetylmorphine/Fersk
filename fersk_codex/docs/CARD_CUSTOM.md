# 标题 `header`

卡片顶部标题区（主/副标题、后缀标签、图标、主题色）。**Card 2.0**。挂在卡片根的 `header` 键下，不在 `body.elements` 内，单卡仅一个。

## 最小示例

```json
{
  "header": {
    "title": { "tag": "plain_text", "content": "卡片标题" },
    "template": "blue"
  }
}
```

## 字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `title` | 是 | Object | 主标题，`{tag:"plain_text"\|"lark_md", content}`，最多 4 行 |
| `subtitle` | 否 | Object | 副标题，同 title，最多 1 行；只配副标题会按主标题展示 |
| `template` | 否 | String | 主题色枚举，见下；默认 `default` |
| `text_tag_list` | 否 | Array | 后缀标签，最多 3 个，每项 `{tag:"text_tag", text:{tag:"plain_text",content}, color}` |
| `i18n_text_tag_list` | 否 | Object | 多语言后缀标签；与 `text_tag_list` 二选一，同配以多语言为准 |
| `icon` | 否 | Object | 前缀图标（同 `div.icon`） |
| `padding` | 否 | String | 内边距，默认 12px，[0,99]px |

**template 枚举**（13 色）：`blue` / `wathet` / `turquoise` / `green` / `yellow` / `orange` / `red` / `carmine` / `violet` / `purple` / `indigo` / `grey` / `default`。

**标签 color 枚举**：`neutral`/`blue`/`turquoise`/`lime`/`orange`/`violet`/`indigo`/`wathet`/`green`/`yellow`/`red`/`purple`/`carmine`。深浅档位及 RGBA 见 `../resource/colors.md`。

## 选色建议

按场景选 template 颜色见 `../lark-im-card-style.md` 意图表。常见语义：green=成功/完成，orange=警告，red=错误/危险，grey=失效/归档，blue=通用信息。


## JSON-REFERENCE
```
{
    "schema": "2.0", // 卡片 JSON 结构的版本。默认为 1.0。要使用 JSON 2.0 结构，必须显示声明 2.0。
    "config": {
        "streaming_mode": true, // 卡片是否处于流式更新模式，默认值为 false。
        "streaming_config": {}, // 流式更新配置。详情参考下文。
        "summary": {  // 卡片摘要信息。可通过该参数自定义客户端聊天栏消息预览中的展示文案。
            "content": "自定义内容", // 自定义摘要信息。如果开启了流式更新模式，该参数将默认为“生成中”。
            "i18n_content": { // 摘要信息的多语言配置。了解支持的所有语种。参考配置卡片多语言文档。
                "zh_cn": "",
                "en_us": "",
                "ja_jp": ""
            }
        },
        "locales": [ // JSON 2.0 新增属性。用于指定生效的语言。如果配置 locales，则只有 locales 中的语言会生效。
            "en_us",
            "ja_jp"
        ],
        "enable_forward": true, // 是否支持转发卡片。默认值为 true。
        "update_multi": true, // 是否为共享卡片。默认值为 true，JSON 2.0 暂时仅支持设为 true，即更新卡片的内容对所有收到这张卡片的人员可见。
        "width_mode": "fill", // 卡片宽度模式。支持 "compact"（紧凑宽度 400px）模式 或 "fill"（撑满聊天窗口宽度）模式。默认不填时的宽度为 600px。
        "use_custom_translation": false, // 是否使用自定义翻译数据。默认值 false。为 true 时，在用户点击消息翻译后，使用 i18n 对应的目标语种作为翻译结果。若 i18n 取不到，则使用当前内容请求翻译，不使用自定义翻译数据。
        "enable_forward_interaction": false, // 转发的卡片是否仍然支持回传交互。默认值 false。
        "style": { // 添加自定义字号和颜色。可应用在组件 JSON 数据中，设置字号和颜色属性。
            "text_size": { // 分别为移动端和桌面端添加自定义字号，同时添加兜底字号。用于在组件 JSON 中设置字号属性。支持添加多个自定义字号对象。
                "cus-0": {
                    "default": "medium", // 在无法差异化配置字号的旧版飞书客户端上，生效的字号属性。选填。
                    "pc": "medium", // 桌面端的字号。
                    "mobile": "large" // 移动端的字号。
                }
            },
            "color": { // 分别为飞书客户端浅色主题和深色主题添加 RGBA 语法。用于在组件 JSON 中设置颜色属性。支持添加多个自定义颜色对象。
                "cus-0": {
                    "light_mode": "rgba(5,157,178,0.52)", // 浅色主题下的自定义颜色语法
                    "dark_mode": "rgba(78,23,108,0.49)" // 深色主题下的自定义颜色语法
                }
            }
        }
    },
    "card_link": {
        // 指定卡片整体的跳转链接。
        "url": "https://www.baidu.com", // 默认链接地址。未配置指定端地址时，该配置生效。
        "android_url": "https://developer.android.com/",
        "ios_url": "https://developer.apple.com/",
        "pc_url": "https://www.windows.com"
    },
    "header": {
        "title": {
            // 卡片主标题。必填。要为标题配置多语言，参考配置卡片多语言文档。
            "tag": "plain_text", // 文本类型的标签。可选值：plain_text 和 lark_md。
            "content": "示例标题" // 标题内容。
        },
        "subtitle": {
            // 卡片副标题。可选。
            "tag": "plain_text", // 文本类型的标签。可选值：plain_text 和 lark_md。
            "content": "示例文本" // 标题内容。
        },
        "text_tag_list": [
            // 标题后缀标签，最多设置 3 个 标签，超出不展示。可选。
            {
                "tag": "text_tag",
                "element_id": "custom_id", // 操作元素的唯一标识。用于在调用组件相关接口中指定元素。需开发者自定义。
                "text": {
                    // 标签内容
                    "tag": "plain_text",
                    "content": "标签 1"
                },
                "color": "neutral" // 标签颜色
            }
        ],
        "i18n_text_tag_list": {
            // 多语言标题后缀标签。每个语言环境最多设置 3 个 tag，超出不展示。可选。同时配置原字段和国际化字段，优先生效多语言配置。
            "zh_cn": [],
            "en_us": [],
            "ja_jp": [],
            "zh_hk": [],
            "zh_tw": []
        },
        "template": "blue", // 标题主题样式颜色。支持 "blue"|"wathet"|"turquoise"|"green"|"yellow"|"orange"|"red"|"carmine"|"violet"|"purple"|"indigo"|"grey"|"default"。默认值 default。
        "icon": { // 前缀图标。
            "tag": "standard_icon", // 图标类型。
            "token": "chat-forbidden_outlined", // 图标的 token。仅在 tag 为 standard_icon 时生效。
            "color": "orange", // 图标颜色。仅在 tag 为 standard_icon 时生效。
            "img_key": "img_v2_38811724" // 图片的 key。仅在 tag 为 custom_icon 时生效。
        },
        "padding": "12px 8px 12px 8px" // 标题组件的内边距。JSON 2.0 新增属性。默认值 "12px"，支持范围 [0,99]px。    
    },
    "body": { // 卡片正文。
        // JSON 2.0 新增布局类属性，用于控制子元素排列：
        "direction": "vertical", // 正文或容器内组件的排列方向。可选值："vertical"（垂直排列）、"horizontal"（水平排列）。默认为 "vertical"。
        "padding": "12px 8px 12px 8px", // 正文或容器内组件的内边距，支持范围 [0,99]px。    
        "horizontal_spacing": "3px", // 正文或容器内组件的水平间距，可选值："small"(4px)、"medium"(8px)、"large"(12px)、"extra_large"(16px)或[0,99]px。
        "horizontal_align": "left", // 正文或容器内组件的水平对齐方式，可选值："left"、"center"、"right"。默认值为 "left"。
        "vertical_spacing": "4px", // 正文或容器内组件的垂直间距，可选值："small"(4px)、"medium"(8px)、"large"(12px)、"extra_large"(16px)或[0,99]px。
        "vertical_align": "center", // 正文或容器内组件的垂直对齐方式，可选值："top"、"center"、"bottom"，默认值为 "top"。
        "elements": [ // 在此传入各个组件的 JSON 数据，组件将按数组顺序纵向流式排列。
            {
                "tag": "xxx", // 组件的标签。
                "margin": "4px", // 组件的外边距，默认值 "0"，支持范围 [-99,99]px。JSON 2.0 新增属性。
                "element_id": "custom_id" // 操作组件的唯一标识。JSON 2.0 新增属性。用于在调用流式更新相关接口中指定组件。在同一张卡片内，该字段的值全局唯一。仅允许使用字母、数字和下划线，必须以字母开头，不得超过 20 字符。
            }
        ]
    }
}
```