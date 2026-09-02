# Which model to run on 2× RTX A6000

## The recommendation

```
MODEL_ID=Qwen/Qwen3-Coder-30B-A3B-Instruct
```

**Qwen3-Coder-30B-A3B-Instruct** — a Mixture-of-Experts model with ~30 B total
parameters but only ~3 B active per token. It is the best fit for this exact
box, for four reasons:

1. **It fits in BF16 with room to spare.** No quantisation, so no quality
   argument to have with yourself later (see the arithmetic below).
2. **It is fast.** Only ~3 B parameters are active per token, so decode
   throughput is closer to a 3 B dense model than to a 32 B one. That matters
   more than raw benchmark scores for a coding assistant, because an agent
   makes many sequential tool-call round trips and you feel every one.
3. **Native tool calling.** Agent mode in Cline/Roo/Continue is only as good as
   the model's structured function-calling, and vLLM ships a dedicated
   `qwen3_coder` parser for it.
4. **Long context.** 256 K natively, and the default `MAX_MODEL_LEN=131072`
   here is a deliberate compromise that leaves KV-cache headroom for several
   concurrent requests rather than one enormous one.

## Why not FP8, and why BF16 is the right call

The RTX A6000 is Ampere (GA102, compute capability **8.6**). FP8 tensor cores
arrived with Hopper and Ada (8.9+). So on this box:

- **BF16** — full hardware support. Use it.
- **INT4 (AWQ / GPTQ via Marlin)** — works on Ampere, and is the escape hatch
  for models too big for BF16.
- **FP8 weights or FP8 KV cache** — not a hardware path here. Leave
  `--kv-cache-dtype` at `auto`.
- **MXFP4** (what `gpt-oss` ships in) — needs Hopper/Blackwell. On Ampere it
  gets dequantised to BF16, which puts a 120 B model far outside 96 GB.

## The VRAM arithmetic

Two A6000s give **96 GB** total (48 GB each). At `TENSOR_PARALLEL_SIZE=2` both
the weights and the KV cache are sharded across the pair.

```
Weights      30.5 B params x 2 bytes (BF16)  =  ~61 GB total  ->  ~31 GB / card
Budget       48 GB x GPU_MEMORY_UTILIZATION 0.90 =  ~43 GB / card
Left for KV  43 - 31                          =  ~12 GB / card  (~24 GB total)
```

KV cache cost per token, from the architecture (48 layers, 4 KV heads,
head_dim 128, K and V, 2 bytes each):

```
48 x 4 x 128 x 2 x 2 bytes  =  ~96 KB per token (across both cards)
131,072 tokens              =  ~12 GB for one completely full context
```

So ~24 GB of KV cache holds roughly two maxed-out 128 K conversations, or —
far more typically — a dozen-plus real editor sessions of 10–20 K tokens each.
With `--enable-prefix-caching` on (it is, in `VLLM_EXTRA_ARGS`) the repeated
system prompt and file context across an agent's tool-call loop is shared
rather than recomputed, which is a large real-world win.

### Check your interconnect before committing to TP=2

The arithmetic above assumes tensor parallelism is viable, which depends on the
link between the cards rather than on the cards themselves:

```bash
nvidia-smi topo -m      # NV1/NV2 = NVLink bridge fitted
nvidia-smi nvlink -s    # "all links are inActive" = no bridge
```

`NV#` — go ahead, TP=2 as documented.

`PHB`, `NODE` or `SYS` (PCIe via the host bridge, no NVLink), **or GPUs passed
through to a VM** — TP=2 all-reduces once per layer over a link that may not
sustain peer-to-peer, which shows up as an `EngineDeadError` half an hour into
real use rather than as a startup failure. Set `NCCL_P2P_DISABLE=1` and
`--disable-custom-all-reduce`, and consider one of these instead:

| Configuration | Trade |
| --- | --- |
| `TENSOR_PARALLEL_SIZE=1`, `PIPELINE_PARALLEL_SIZE=2` | Point-to-point at one layer boundary instead of a per-layer all-reduce. Slightly higher latency, far more robust. Keeps BF16 and the full 30B. |
| One card, INT4 (`--quantization awq_marlin`) | ~17 GB for a 30B checkpoint, so it fits a single A6000 with a big KV cache and issues no collective at all. Some quality loss; frees the second card for a separate replica. |

`make preflight` checks both the topology and whether it is running on a
hypervisor, and warns accordingly. Details in
[OPERATIONS.md](OPERATIONS.md#known-failure-modes).

**Do not trust these numbers over the server's own.** vLLM prints the
authoritative figure at startup:

```bash
make logs-vllm | grep -i "KV cache"
# GPU KV cache size: 249,856 tokens
# Maximum concurrency for 131,072 tokens per request: 1.9x
```

If that concurrency number is below ~2, lower `MAX_MODEL_LEN`. It is the single
highest-leverage knob in `.env`.

## Alternatives, and when to pick them

| `MODEL_ID` | Shape | BF16 weights | Pick it when |
| --- | --- | --- | --- |
| `Qwen/Qwen3-Coder-30B-A3B-Instruct` | MoE 30B / 3B active | ~61 GB | **Default.** Best speed-per-quality here, strong tool calling. |
| `mistralai/Devstral-Small-2507` | 24B dense | ~48 GB | You want a dense model tuned specifically for SWE-agent-style loops, and the largest KV headroom of the lot. |
| `Qwen/Qwen2.5-Coder-32B-Instruct` | 32B dense | ~64 GB | You need proven fill-in-the-middle for autocomplete from the *same* model, and care less about agentic tool use. |
| `zai-org/GLM-4.5-Air` | MoE 106B / 12B active | ~212 GB | Quality above all, and you accept INT4. Needs an AWQ/GPTQ repo plus `--quantization awq_marlin`; expect to cut `MAX_MODEL_LEN`. |
| `Qwen/Qwen3-Coder-480B-A35B-Instruct` | MoE 480B | — | Never, on this box. Listed so you don't try. |

Switching is a two-line change:

```bash
sed -i 's|^MODEL_ID=.*|MODEL_ID=mistralai/Devstral-Small-2507|' .env
sed -i 's|^VLLM_TOOL_ARGS=.*|VLLM_TOOL_ARGS=--enable-auto-tool-choice --tool-call-parser mistral|' .env
make pull-model && make restart && make wait && make smoke
```

### Match the tool-call parser to the model family

This is the most common cause of "the model chats fine but agent mode does
nothing":

| Family | `VLLM_TOOL_ARGS` |
| --- | --- |
| Qwen3-Coder | `--enable-auto-tool-choice --tool-call-parser qwen3_coder` |
| Qwen3, Qwen2.5 | `--enable-auto-tool-choice --tool-call-parser hermes` |
| Devstral / Mistral | `--enable-auto-tool-choice --tool-call-parser mistral` (add `--tokenizer-mode mistral` to `VLLM_EXTRA_ARGS`) |
| Llama 3.x | `--enable-auto-tool-choice --tool-call-parser llama3_json` |
| GLM-4.5 | `--enable-auto-tool-choice --tool-call-parser glm45` |

`make smoke` asserts that a tool call actually comes back, so a mismatch fails
loudly instead of silently degrading your editor.

## The autocomplete companion

Inline tab-completion has a different requirement from chat: it must answer in
under ~200 ms, and it needs true fill-in-the-middle (prefix *and* suffix), not
chat. Sharing the 30 B model for both works but feels sluggish.

Enabling the second service costs ~4 GB of VRAM and gives you a dedicated FIM
model:

```bash
sed -i 's/^AUTOCOMPLETE_ENABLED=.*/AUTOCOMPLETE_ENABLED=true/' .env
make up PROFILES="--profile autocomplete"
make vscode-config     # now emits an autocomplete role too
```

The gateway routes by model name: requests for `AUTOCOMPLETE_MODEL_ID` (or the
literal alias `autocomplete`) go to the small server, everything else to the
big one. One base URL, one API key, two models.
