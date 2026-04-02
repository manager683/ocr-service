# Cloud Run OCR Service

This service is the optional OCR layer for the receipt Apps Script project.

It is designed for the current Apps Script client in:

- [20_gemini.gs](C:\Users\THOMAS\Desktop\New folder (5)\thomas 的 帐目\20_gemini.gs)

When deployed, Apps Script can call this service first for batch OCR, and automatically fall back to direct Gemini if the service is unavailable.

## What it does

- Accepts a batch of receipt or Shopee images
- Preprocesses images before OCR
- Uses multiple Gemini API keys on the server
- Returns structured JSON in the exact shape expected by Apps Script

## Endpoint

- `GET /ocr-batch`
  - health check
- `POST /ocr-batch`
  - batch OCR

## Request shape

```json
{
  "task": "receipt",
  "prompt": "custom prompt from Apps Script",
  "model": "gemini-2.5-flash",
  "items": [
    {
      "file_name": "1.jpg",
      "mime_type": "image/jpeg",
      "base64_data": "..."
    }
  ]
}
```

Supported `task` values:

- `receipt`
- `receipt_date`
- `shopee`
- `shopee_date`

## Response shape

```json
{
  "success": true,
  "task": "receipt",
  "count": 1,
  "results": [
    {
      "ok": true,
      "data": {
        "date": "2026-03-22",
        "store": "MR D.I.Y",
        "ssm": "111111-X",
        "receipt_no": "R0001",
        "total": "16.00"
      }
    }
  ]
}
```

## Environment variables

Required:

- `API_KEYS`
  - multiple Gemini keys
  - one per line, or comma-separated

Optional:

- `API_KEY`
- `API_KEY_1`, `API_KEY_2`, ...
- `MODEL`
  - default: `gemini-2.5-flash`
- `MAX_WORKERS`
  - default: `6`
- `RECEIPT_UPSCALE_FACTOR`
  - default: `2.0`
- `SHOPEE_UPSCALE_FACTOR`
  - default: `1.2`
- `GEMINI_TIMEOUT_SECONDS`
  - default: `90`
- `BEARER_TOKEN`
  - if set, Apps Script must send the same token in `OCR_BATCH_TOKEN`

## Deploy with gcloud

From this folder:

```powershell
cd "C:\Users\THOMAS\Desktop\New folder (5)\thomas 的 帐目\cloud-run-ocr"
gcloud run deploy receipt-ocr-batch `
  --source . `
  --region asia-southeast1 `
  --allow-unauthenticated `
  --set-env-vars "API_KEYS=YOUR_KEY_1,YOUR_KEY_2,YOUR_KEY_3"
```
If you want token protection:

```powershell
gcloud run deploy receipt-ocr-batch `
  --source . `
  --region asia-southeast1 `
  --allow-unauthenticated `
  --set-env-vars "API_KEYS=YOUR_KEY_1,YOUR_KEY_2,YOUR_KEY_3,BEARER_TOKEN=YOUR_SECRET_TOKEN"
```
After deploy, Cloud Run will give you a URL like:

- `https://receipt-ocr-batch-xxxxx-uc.a.run.app`

Use this in Apps Script Script Properties:

- `OCR_BATCH_URL=https://receipt-ocr-batch-xxxxx-uc.a.run.app/ocr-batch`
- `OCR_BATCH_TOKEN=YOUR_SECRET_TOKEN` if you enabled token protection

## Apps Script side

The Apps Script project already supports:

- `OCR_BATCH_URL`
- `OCR_BATCH_TOKEN`

If `OCR_BATCH_URL` is blank:

- Apps Script uses direct Gemini

If `OCR_BATCH_URL` is set:

- Apps Script uses Cloud Run OCR first
- if Cloud Run fails, Apps Script automatically falls back to direct Gemini

## Recommended first test

1. Deploy the service
2. Open the service URL with `/ocr-batch`
3. Confirm you see a JSON health response
4. Put the URL into Apps Script Script Properties as `OCR_BATCH_URL`
5. Run `Expense Tools -> Test Gemini API`
6. Confirm the dialog shows:
   - `Active OCR mode: Cloud Run OCR + Gemini fallback`
