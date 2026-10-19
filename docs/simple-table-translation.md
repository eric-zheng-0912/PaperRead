# 簡易表格翻譯實作與驗收

此功能翻譯使用者選定的 PDF 文字區域，與精確表格重建分離。原始 PDF 保留，中文以獨立純文字呈現；所有範圍均標示整表完整性未確認。它不證明跨頁表格完整，也不推斷儲存格關係。

交付預設：簡易翻譯的閱讀器入口正常可用；舊精確擷取預設停用。僅在實驗環境明確設定 `PAPERREAD_DISABLE_TABLE_EXTRACTION=0` 才啟用舊流程。全文翻譯在停用時略過舊精確表格後處理，簡易翻譯不受此開關影響。原精確 regression 以測試套件明確 opt-in 執行；不把實驗品質閘門當作新流程的完整性保證。

## 使用方式

1. 在閱讀器開啟右側 PDF，點「簡易表格翻譯」；既有 table 區塊的操作列亦有入口。
2. 追加本頁自動候選，或開啟「手動框選／追加」後在 PDF 拖曳。可以換頁追加另一個範圍，亦可移除後重選。
3. 點「預覽英文」，對照每個範圍的 PDF 裁圖與英文分行文字。
4. 點「翻譯為繁體中文」。結果依範圍分頁顯示原圖、英文及中文；可取消工作。重開閱讀器可從「已儲存的簡易譯文」載入。

自動候選只是一個待核對範圍，不會自行送譯或標為完整表格。使用者必須檢查 Caption、表內文字及附註是否涵蓋；跨頁內容可以追加，追加後必須重新預覽。

## 程式與儲存範圍

- `easyread/simple_tables.py`：候選、區域文字擷取、英文／PDF 預覽、獨立儲存與快取、驗證後讀取，以及 API 操作分派。複用現有 Caption、PDF 行文字及可見頁框處理；不呼叫 `_matrix()`、`recognize()` 或以精確擷取失敗原因決定能否翻譯。
- `easyread/table_translate.py`：把原有 reading 的分段遮罩翻譯抽為 `translate_segments()`；舊 reading 流程繼續使用相同來源驗證、拒絕條件及提交方式。
- `easyread/server.py`、`easyread/jobs.py`：新增經既有 X-Token 保護的 `/api/p/{id}/simple-table` POST 操作，與既有小工作佇列／取消機制整合。
- `easyread/reader_data.py`：不把未經新 API 驗證的原始快取送進一般 reader／離線匯出資料。
- `easyread/web/js/reader/simple-tables.js`：實際候選、指標拖曳選框、跨頁追加、英文預覽、翻譯、取消、工作輪詢及重開載入介面。模型文字只用 `textContent`；來源／譯文區域排除 DOM 自動介面翻譯。
- `actions.js`、`pageview.js`、`reader.html`、`reader.css`：接上入口、目前 PDF 頁碼及面板樣式；`web/i18n/en.json` 補齊新介面／錯誤訊息英文。
- `tests/test_simple_tables.py`：9 個隔離驗收測試；複用既有隔離測試配置及 Mock 回應。
- `scripts/table_qa/simple_review.py`、`tests/simple_tables_browser.cjs`：每次建立全新隔離 profile 的實際 HTTP／工作佇列／Chromium 驗收，不下載模型或瀏覽器。

獨立資料儲存於 `paper.simple_table_translations`，不修改舊 `blocks`、矩陣、Caption 譯文或整頁翻譯狀態。來源雜湊涵蓋 PDF bytes、版本、各頁選框及分段英文；翻譯快取另綁定模型參數、繁中目標、術語表與輸出雜湊。選框或來源變更必須重新預覽；回應提交前再次驗證並以既有儲存鎖原子更新。追加範圍會形成新的獨立紀錄，舊結果保留。

## 保護與限制

無格線、無數字、合併表頭或無法對齊儲存格，都不構成新流程的拒絕理由。來源依 PDF 文字層分行，不重建排版；只有明確的拉丁連字可在字元比較時展開，不做廣泛相容正規化。

選框切到 glyph、重疊範圍、不可解碼或旋轉文字、空文字層、來源變更、模型缺段／順序或數字符號變動均拒絕，保留 PDF 與既有結果。每次最多 12 個範圍、60,000 個文字字元；過大範圍需分批。沒有 OCR，因此掃描表格仍只能查看原始 PDF。PDF 文字層本身的閱讀順序不一定等同複雜數學排版；此功能不推斷視覺語意。

已儲存結果可在本機服務閱讀器重開使用；本輪沒有加入獨立 HTML 離線匯出的簡易譯文檢視。來源核對與數字符號保護不等於模型語意翻譯品質保證，實際模型品質未以付費呼叫驗證。

## 驗收紀錄

9 個新增測試涵蓋無格線概念表、換行與合併表頭、無 Caption 的格線候選、跨頁追加、獨立快取與模型參數失效、重開載入、既有論文／其他文件／筆記／設定／PDF 保留、過期或不合法選框、取消／模型錯誤／來源變更、符號保留、掃描影像及工作取消範圍。

真實 headless Chromium 已從閱讀器按鈕選擇第一頁候選、查看英文，再以實際指標拖曳追加第二頁，取得兩頁繁中 Mock 譯文。第二次翻譯命中快取，Mock 呼叫總數維持 1；重開可找回譯文；舊正文、舊表格及筆記保留，僅一般閱讀進度照常更新。瀏覽器亦確認錯誤 X-Token 回傳 403，且無 renderer 例外。此為真實 UI／HTTP／工作流程驗收，模型回應為固定字詞翻譯，沒有真實模型呼叫。

最近瀏覽器證據位於 `.paperread-local-test/simple-table-browser-t_yldhp5/` 的 `browser-report.json` 與 `reader-result.png`；方便檢視的截圖另在 `tmp/simple-table-reader-result.png`。已檢視實際畫面。

既有 37 個表格前端測試通過。固定精確流程 regression 仍為 7 精確接受、9 FN、5 正確 fallback、3 正確拒絕、1 模糊接受、1 未標記跨頁不安全接受，無新增正文 FP 或儲存格錯位；原實驗性品質閘門仍為 exit 2，資料在 `tmp/simple-table-exact-regression.json`。未修改精確擷取規則、ground truth 或基準快照，也未開封 holdout。新功能不把該失敗洗成成功。

首次完整 Python 回歸 597 項，596 通過，唯一失敗為新增介面尚未補齊 i18n；該失敗 log 保留於 `tmp/simple-table-full-tests-initial-i18n-failure.log`。補齊後 i18n 與新增安全案例共 18 項通過；最終完整 Python 回歸 **600 項全部通過（74.047 秒，exit 0）**，記於 `tmp/simple-table-full-tests.log`，隔離 profile 為 `.paperread-local-test/stability-suite-7pv8zxvt/`。既有隔離全套 launcher 複用為 `tmp/run_simple_table_suite.py`，僅更換此次 log 名稱，未覆寫先前 table-stability 驗收紀錄。

重現命令：

```powershell
.venv/Scripts/python.exe -m unittest tests.test_simple_tables tests.test_i18n
# 使用既有 Playwright 與本機 Chrome，不進行安裝或下載。
$env:NODE_PATH='C:/Users/00818/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
& 'C:/Users/00818/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe' tests/simple_tables_browser.cjs
```

所有驗收使用新建隔離資料、Mock 模型與外部連線阻擋；正式文獻庫、設定、Vault、安裝版及 Git 備份未操作。既有未追蹤的跨頁複核報告與 tmp 紀錄保留；沒有 Commit、Push 或 Merge。
