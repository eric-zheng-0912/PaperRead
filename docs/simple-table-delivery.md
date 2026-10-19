# 簡易表格翻譯交付驗收

## 交付設定

- 簡易表格翻譯可由閱讀器 PDF 面板與表格區塊操作列啟動，獨立於精確表格辨識。
- `table_policy.enabled()` 在沒有環境變數時回傳 false，舊精確重建預設停用；實驗測試明確 opt-in。
- `jobs.py` 與 CLI 的全文翻譯完成後，只在精確流程啟用時才執行其後處理；不讓停用狀態造成一般全文翻譯失敗。
- 舊精確表格跨頁辨識規則未修改，未重新處理該問題。

## 封裝與驗收隔離

`scripts/table_qa/build_simple_desktop.py` 沿用既有依賴檢查及 SSL DLL 收集方式，重新編譯 Python 後端、封裝 Electron 到每次全新建立的 `dist/table-review/simple-delivery-*` 目錄。正式 `scripts/build_backend.py` 與安裝版不改寫；不執行安裝器、不註冊協定、不發布。

封裝使用已存在的 `node_modules/electron/dist`，並透過封裝器內建的 traversal collector 收集依賴；不更改 node_modules 原始碼。早期下載／npm 失敗的產物與 log 保留，不當成成功。專用 runtime hook 僅由隔離建置指定，正常產品 builder 不載入它；該測試 exe 拒絕沒有專用 marker 的 profile，阻擋真實模型 CLI 與外部 socket。

`prepare_simple_delivery.py` 每次建立新的 `.paperread-local-test/simple-delivery-*` profile，使用原有 NIST TN 2113 三頁真實 PDF fixture，不修改原檔或 truth。fixture 中包含已翻譯正文、legacy 表格、既有筆記與待補譯正文，搭配明確 Mock 回應測試資料。

`tests/simple_tables_desktop.cjs` 使用實際封裝後的 PaperRead.exe、實際 Python 服務、工作佇列及 Electron renderer。測試啟動參數使用軟體繪製並停用 Chromium sandbox，以適應此隔離測試環境；正式應用程式啟動參數未修改。

## 必要測試

- 完整隔離 Python 回歸：602 項通過，75.825 秒，exit 0；`tmp/simple-delivery-full-tests.log`。
- 表格前端：37 項通過，exit 0。
- 新增開關測試確認：無環境變數時精確流程停用；停用時簡易文字翻譯仍可完成；全文翻譯不呼叫停用的精確後處理。
- 桌面驗收：通過，exit 0。實際封裝 exe 為 `dist/table-review/simple-delivery-ytsovfh0/win-unpacked/PaperRead.exe`；使用全新 `.paperread-local-test/simple-delivery-1mrzaxyv/` profile 與真實 NIST PDF。完整結果保留於 `tmp/simple-delivery-desktop-report.json` 及 profile 的 `desktop-report.json`。
- 通過項目：精確流程停用但簡易入口可用、自動候選、英文預覽、實際指標框選、跨頁追加、繁中純文字、獨立快取、四種閱讀模式、建立與保存筆記、一般全文翻譯、實際關閉／重新啟動桌面版、既有資料與 PDF／設定保留。
- Mock 呼叫紀錄只有 1 次簡易翻譯與 1 次全文翻譯；重複簡易翻譯與重新開啟未增加模型呼叫。沒有真實 API、Gemini 或其他付費模型呼叫。
- 最後一輪僅重跑失敗桌面端到端案例與 2 個直接相關測試，均通過；未重跑完整 602／37 項測試。`git diff --check` 通過。

## 最後 Mock 接線修正（2026-10-19）

原 hook 僅替換 `engines.run`。簡易翻譯直接呼叫該函式而成功，但 `translate.translate_pages()` 在此之前還會呼叫 `engines.require_model_access()`，因此全文翻譯被正常的隔離保護攔截。

只修正測試 hook：為限定的 `delivery-body`、隔離 library 與第 2 頁全文翻譯 fixture 提供具有 Mock preflight 的函式相依物件。執行前必須確認 `engines.run` 仍是固定 Mock、底層 `_run`／`_popen` 仍被阻擋、外部 socket 阻擋仍存在，而且正式隔離保護仍拒絕模型存取。沒有更改 `engines.py`、`translate.py` 或 `review_policy.py`；正式 `engines.require_model_access()` 完全不變。

新增的直接回歸測試在獨立子程序中實際跑全文補譯，並確認正式保護在前後都拋出 EngineError；其他頁碼、替換 Mock 傳輸及底層真實傳輸皆被拒絕。

沿用既有 Electron 封裝、Python 模組封存與全部依賴，只重新封裝測試 hook 所在的後端入口；沒有重建整套功能或 Electron。舊測試後端入口備份保留於 `build/mock-hook-refresh-v9xvqvne/previous-easyread-backend.exe`；Electron `app.asar` 未變，SHA-256 為 `911347a07c4d158f9d0a2d9b8a41072ec24ad12d1c6dd34295db890a70bf29dc`。

Mock 譯文用於驗證完整工作流程、分段與數字符號保護，不作為語意翻譯品質證據。遇到 Mock 重新排列遮罩導致符號檢查失敗時，只修正 Mock，沒有放寬正式驗證條件。

## 提交範圍

精確清單為 `docs/simple-table-commit-files.txt`。只提交功能程式、測試、驗收工具與文件；`tmp/`、`.cache/`、`build/`、`dist/`、隔離 profile、正式文獻庫與設定均由 ignore 規則排除。先前未追蹤的 `docs/table-crosspage-review.md` 保留但不列入本次提交。沒有執行 git add、Commit、Push 或 Merge。

使用者下一步（在 feature/table-stability）：

```powershell
git add --pathspec-from-file=docs/simple-table-commit-files.txt
git diff --cached --check
git commit -m "Add simple table translation and disable exact reconstruction by default"
git push origin feature/table-stability
```

重現隔離桌面驗收：

```powershell
.venv/Scripts/python.exe -m scripts.table_qa.build_simple_desktop
.venv/Scripts/python.exe -m scripts.table_qa.prepare_simple_delivery
$env:NODE_PATH='C:/Users/00818/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
& 'C:/Users/00818/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe' tests/simple_tables_desktop.cjs
```
