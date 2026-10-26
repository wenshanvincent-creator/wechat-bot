# Tokenizer provenance

Downloaded on 2026-10-25 from the official DeepSeek token-usage documentation:
https://api-docs.deepseek.com/quick_start/token_usage

Archive: https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip
Only tokenizer.json and tokenizer_config.json were extracted. No downloaded
Python scripts are executed; no model weights or inference runtime are used.

SHA-256:

- tokenizer.json: 89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614
- tokenizer_config.json: 841f8cf146e3f0ad1082594a31f68ecf7608c20467ef355081333d85bbaeb1cb

The archive's model_max_length is tokenizer metadata, not the live model's context
capacity. The configured 1,000,000-token model budget comes from the current official
model documentation: https://api-docs.deepseek.com/quick_start/pricing

Local counts use the archive's two-message chat template. Observed API prompt_tokens
calibrates positive server overhead. Encoding uses 16,384-character chunks to bound
memory; merges at chunk edges can slightly change counts. API usage remains the
authoritative measurement.
