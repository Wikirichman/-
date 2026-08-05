# 国内期货选品与监控原型

这个小程序用于从国内期货品种池中选出约 3 个“供需矛盾突出、盘面表现突出”的龙头候选。它不是交易建议，而是一个可解释的监控框架：每天更新数据后，按同一套规则做横截面排名，并输出方向、分数和入选理由。

## 评分逻辑

综合分由三部分构成，并且多头、空头分开计算：

- 盘面强弱：20 日涨跌幅、60 日涨跌幅、20/60 日均线差、60 日区间位置。
- 供需矛盾：库存 30 日变化。库存下降偏向多头矛盾，库存上升偏向空头矛盾。
- 技术/资金确认：持仓 20 日明显增加与近期成交量放大。多头重点看“上涨增仓”，空头重点看“下跌增仓”。
- 小时级别触发：多头识别“低点抬高后突破摆动高点”的 N 字反转或震荡中枢上破；空头识别“高点降低后跌破摆动低点”的 N 字反转或震荡中枢下破。

程序会分别计算 `long_score` 和 `short_score`，取更强的一侧作为该品种方向，再输出全市场排名靠前的品种。这样不会用多头理由解释空头，也不会把单纯缩量反弹误认为资金确认。

小时结构默认作为入选门槛：只保留小时线已经触发、且方向和日线逻辑一致的品种。把 `config/universe.json` 里的 `intraday.required_for_selection` 改为 `false`，则小时结构只作为加分与提示。

最终入选默认要求持仓 20 日增加至少 5%，可通过 `positioning.min_oi_change_20d` 调整。它把“上涨增仓”或“下跌增仓”从普通加分项提升为资金确认门槛。

最终入选也默认要求供需方向一致：多头库存下降，空头库存上升；没有可靠库存数据的品种不因库存缺失被直接剔除。

品种池已经限定为主流连续合约，因此默认不再用横截面平均成交量过滤候选。需要额外剔除低流动性品种时，可调高 `min_liquidity_score`。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 离线试跑

```bash
python -m futures_monitor.cli --provider sample
```

## 网页版

```bash
python3 -m futures_monitor.web
```

启动后打开 `http://127.0.0.1:8787`。页面里可以直接运行样例或 AkShare 实盘，并下载 CSV。
本地开发默认账号是 `admin`，密码是 `local-dev`。公网部署时必须通过环境变量设置正式密码和密钥。

在 macOS 上也可以双击 `启动期货监控网页.command`，然后打开 `http://127.0.0.1:8787`。

## 公网部署

项目已经包含 `Dockerfile` 和 `render.yaml`，可以部署到 Render、Railway、Fly.io 或云服务器。

最省钱的第一步是 Render 免费 Web Service：把项目上传到 GitHub，在 Render 里选择 Blueprint 部署，并设置：

- `FUTURES_MONITOR_USERNAME`: 登录账号
- `FUTURES_MONITOR_PASSWORD`: 登录密码
- `FUTURES_MONITOR_SECRET_KEY`: 长随机密钥
- `FUTURES_MONITOR_COOKIE_SECURE`: `1`

完整步骤见 `DEPLOY.md`。

## 使用 AkShare 实盘数据

```bash
python -m futures_monitor.cli --provider akshare --output outputs/latest_selection.csv
```

如果某个品种的库存接口不可用，程序会保留盘面和资金项评分，库存项按中性处理。

## 数据来源与严谨性

实盘模式只使用公开可复核数据，并把来源链路直接展示在网页里：

- 国内日线、成交量、持仓量：AkShare `futures_zh_daily_sina`
- 国内库存：AkShare `futures_inventory_em` 优先，失败时回退 `futures_inventory_99`
- 小时线：AkShare `futures_zh_minute_sina(period="60")`
- COMEX 库存：AkShare `futures_comex_inventory`
- LME 库存：AkShare `macro_euro_lme_stock`
- 外盘价格：AkShare `futures_foreign_hist`

程序的处理原则是：

- 只根据原始字段计算 20 日涨跌、30 日库存变化、20 日持仓变化和小时结构
- 接口抓取失败就留空，不补值、不猜值
- 供需分析会先展示原始库存与价格变化，再输出解释文字

`sample` provider 仅用于离线演示，不能用于实盘判断。

## 配置品种池

编辑 `config/universe.json`。每个品种包含：

- `symbol`: AkShare/Sina 连续合约代码，例如 `CU0`、`RB0`。
- `name`: 中文名。
- `sector`: 板块。
- `inventory_symbol`: 库存接口里的品种名；没有可靠库存数据时可以留空。

## 下一步可以增强

- 接入交易所仓单、钢联/隆众/卓创等产业数据源。
- 加入季节性、基差、月差、利润、进口利润和产能利用率。
- 定时任务推送到企业微信、飞书或邮件。
- 加入风控过滤，例如涨跌停、节假日、主力换月、流动性阈值。
