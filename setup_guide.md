# 美股每日早报 - 配置指南

## 一、创建飞书群机器人

1. 打开飞书，进入你要接收简报的**群聊**
2. 点击右上角 **群设置**（群名称）
3. 找到 **机器人** 选项卡，点击 **添加机器人**
4. 选择 **自定义机器人**
5. 机器人名称填 `美股早报`，可上传头像
6. 点击添加后，复制 **Webhook 地址**（格式：`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx`）
7. 回到本项目，编辑 `config.json`，将 Webhook 地址粘贴到 `feishu_webhook_url` 字段

## 二、本地测试

```bash
# 安装依赖
pip3 install -r requirements.txt

# 预览卡片内容（不发送）
python3 us_daily_briefing.py --dry-run

# 实际发送到飞书
python3 us_daily_briefing.py
```

也可以双击 `us_daily_briefing.command` 文件运行。

## 三、配置 GitHub Actions 自动推送

### 1. 创建 GitHub 仓库

```bash
cd /Users/smzdm/Desktop/MFM/automation
git init
git add .
git commit -m "美股每日早报 - 飞书自动推送"
git remote add origin https://github.com/你的用户名/us-daily-briefing.git
git push -u origin main
```

### 2. 配置 Secret

1. 打开 GitHub 仓库页面 → **Settings** → **Secrets and variables** → **Actions**
2. 点击 **New repository secret**
3. Name: `FEISHU_WEBHOOK_URL`
4. Value: 粘贴你的飞书 Webhook 地址
5. 点击 **Add secret**

### 3. 测试运行

1. 进入 GitHub 仓库 → **Actions** 选项卡
2. 选择 **US Stock Daily Briefing** workflow
3. 点击 **Run workflow** → **Run workflow** 手动触发一次
4. 检查飞书群是否收到消息

### 4. 自动运行

- 每个交易日（周一到周五）北京时间 08:10 自动运行
- 如果当天是美国假期（美股休市），yfinance 会返回上一个交易日的数据，简报照常发送

## 四、自定义

### 修改中概股列表

编辑 `config.json` 中的 `chinese_adrs` 字段，添加或删除股票：

```json
{
    "chinese_adrs": {
        "BABA": "阿里巴巴",
        "NEW_TICKER": "新股票名称"
    }
}
```

### 注意事项

- `config.json` 中的 `feishu_webhook_url` 仅用于本地测试
- GitHub Actions 运行时会从 Secret 中读取 Webhook URL 并覆盖 config.json 中的值
- **不要将 Webhook URL 提交到 Git 仓库**，建议在 `.gitignore` 中添加 `config.json`
