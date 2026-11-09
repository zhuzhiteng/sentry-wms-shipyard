# 船厂仓储微信小程序

该目录是船厂现场端的唯一移动端，复用 Sentry WMS 的账号、角色、仓库权限和
`/api/shipyard` 接口，不维护第二套业务身份。

## 页面

- 收货任务：读取到货清单和票货，展示存放区域；四联验收单先拍照自动读取编号，
  字段齐全时直接留存并继续拍下一张，再逐托拍摄吊牌并一次确认实收。正常收货
  数量默认等于还应到且只读，只有点击“有少货”后才填写实际数量和原因；确认当前
  票后自动进入同清单的下一票。已保存验收单如识别有误，可直接修改信息，不替换原图。
- 出库任务：读取正式出库单和整票任务，展示物料编码、品名、区域及全部托盘后缀；
  逐托拍照 OCR 后一次确认整票出库。
- 装车确认：整张出库单全部票货已出库且无缺口时，单独记录装车人和装车时间。

## 开发与部署

1. 使用微信开发者工具打开本目录。
2. 在 `project.config.json` 中替换正式小程序 AppID；本地可使用测试号。
3. 在 `config.js` 中配置正式 API 根地址。登录页不再让现场人员填写服务器地址；
   正式环境必须使用 HTTPS，并将域名加入微信小程序 request/uploadFile 合法域名。
4. 为操作员配置 `RECEIVER` 或 `OUTBOUND` 角色，并确认账号已绑定仓库。页面权限
   分别需要包含 `inbound` 或 `shipyard-outbound`。正式出库的列表、详情、拍照
   识别、整票核销、装车确认和照片下载同时校验角色与 `shipyard-outbound` 页面
   权限，缺少页面权限会返回 `403`。

本项目默认关闭开发者工具的域名校验，仅用于本地联调。生产构建不得依赖这一设置。

### 生成正式发布包

不要直接把当前开发目录上传为正式版本。当前 `touristappid`、本机 HTTP 地址和
`urlCheck=false` 仅用于本地开发。正式包必须由统一预检生成：

```powershell
node tools/shipyard/release-preflight.mjs `
  --env-file D:\secure\shipyard-production.env `
  --app-id wx_REPLACE_WITH_REAL_APP_ID `
  --api-base https://wms-api.example.com/api `
  --output-dir D:\release\shipyard-miniprogram `
  --report-dir D:\release\preflight
```

生成目录会强制写入正式 AppID、HTTPS API、`urlCheck=true`、版本和文件哈希，
并排除开发测试。只有预检报告为 `READY` 后，才用微信开发者工具打开生成目录并
上传体验版。正式 AppID、合法域名、隐私保护指引和真机验收仍由项目负责人完成。

## 识别边界

正常页面只调用 `wx.chooseMedia` 相机能力完成吊牌拍照和 OCR 留证。
每托拍摄一次，同一张原图上传到 `label-recognition` 接口，既用于 OCR，也作为
收货/出库证据。服务端负责字段比对、冲突定位、候选任务和最终核销；客户端不自行
判断识别结果。

OCR 默认由 API 主机内的 RapidOCR 处理，原图不会发送到外部 OCR 服务。只有数据
负责人批准并配置 `SHIPYARD_OCR_FALLBACK_PROVIDER=baidu` 后，本地识别失败或结果
为空时才会把原图发送到百度；此时必须通过受控环境变量配置
`BAIDU_OCR_API_KEY` 和 `BAIDU_OCR_SECRET_KEY`，不得写入仓库。

## 弱网与恢复

- 收货、整票出库和装车提交前会把幂等键写入本地存储；
- 请求发出前先落盘“提交中”标记；应用被系统中断后按结果未确认恢复并锁定编辑；
- 网络中断、超时、`408` 或 `5xx` 不重新生成键，页面显示“结果未确认”并允许原键重试；
- 收货数量和差异原因、已拍托盘、待补托盘后缀可跨页面重开恢复；
- 明确业务拒绝会清除旧提交键；`409` 会清理旧草稿并刷新服务端状态；
- 草稿只保存在当前设备，12 小时后自动失效。

## 测试

```powershell
npm test
```

测试覆盖一次拍摄、OCR 字段补选托盘后缀、弱网原键重试、草稿恢复、
以及 `app.json` 声明页面的四件套完整性。
