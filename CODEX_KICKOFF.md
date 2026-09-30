# AOS — Astra 6 High kick-off

You are the principal engineer implementing AOS — Agent Operating System in this repository. Read the entire README.md first, then docs/ARCHITECTURE.md, RUNTIME.md, PROTOCOL.md, SECURITY.md, MODELS.md, REGISTRIES.md, TRAINING_DATA_FOUNDATION.md, ROADMAP.md and STATUS.md. These are the current architecture contract. Inspect all migrations, schemas, recipes and examples before designing persistence or dataset interfaces.

Build a fully local computer-use platform targeting CachyOS, NVIDIA RTX 4070 Ti SUPER (16 GB VRAM), and 32 GB RAM. The package is a specification and data foundation, not a working agent. Do not claim runtime, model, training or GUI success from package validation alone.

Preserve two logical agents: Supervisor uses Bonsai-2 27B and vision for planning, diagnosis and recovery; Operator uses Decider-2B, a typed state machine, deterministic tools and safety policies. It is not a second chatbot. Prefer internal APIs, filesystem/process, browser DOM/CDP, accessibility, then vision. Translate Turkish user goals into bounded English decision state without altering filenames, quoted content, authorization or negation. Return user-facing results in Turkish.

Keep GPU inference native on the host: a compatible pinned PrismML llama.cpp CUDA build for Bonsai and a separately managed PyTorch/CUDA Decider service. Do not assume stock llama.cpp supports the selected Bonsai format. Pin checkpoint, tokenizer, projector, code and dependency revisions together. Measure the real model limits and GPU peak usage; treat 16K Bonsai context and simultaneous residency as hypotheses. The Decider card/repository mismatch documented in MODELS.md must be resolved experimentally.

The autonomous computer is Ubuntu/XFCE/X11/noVNC in Docker, with a controlled gateway and explicit workspace mounts. Do not give it unrestricted access to the real host desktop/home or Docker socket. Introduce ComputerRuntime early so KVM/QEMU can replace Docker later. Implement a minimal safe filesystem/gateway sandbox before executing the first action; expand to the full desktop later.

Use small typed components: Supervisor, Operator, DecisionEngine, ModelRuntime, ComputerRuntime, ComputerGateway, ToolRegistry, SafetyPolicy, StateStore, TrajectoryStore, ModelRegistry, AdapterRegistry, DeploymentRegistry, BenchmarkRunner. Python/uv/FastAPI/Pydantic/asyncio/SQLite; Tauri/React/TypeScript/pnpm for the eventual UI. Do not replace the orchestration core with an agent framework. Do not start with a polished UI or a large training platform.

## Phase 0: inspect reality

Inspect the repository and actual machine. Record OS/kernel, NVIDIA/CUDA, available VRAM/RAM/disk, compiler/CMake, Python/uv, Docker/Compose, Node/pnpm, Rust/cargo, existing model files and runtime checkouts. Do not reinstall working dependencies or stop unrelated listeners. If this is not the target CachyOS host, implement and test portable contracts but mark GPU/runtime acceptance blocked. Update docs/STATUS.md with evidence, not assumptions.

Run scripts/validate_package.py in a virtualenv with requirements-validation.txt. Keep the existing contract tests meaningful. If a README assumption fails, document the measured conflict in DECISIONS.md and make the smallest justified adjustment.

## First implementation milestone

Implement configuration, typed canonical state and error taxonomy; migrations and TrajectoryStore repositories; DecisionEngine interface and real Decider adapter; safe filesystem/shell tools behind the minimal gateway; deterministic policy checks; structured logging and a CLI/dev harness.

Acceptance: create `/workspace/hello.txt` containing exactly `Hello from the local agent.\n`, read it back, verify equality, and store the complete run/step/state/decision/action/observation/verification trajectory. No System-2 call is necessary. A fake DecisionEngine may support tests, but it must be labeled and does not satisfy real-model acceptance.

Persist action intent before execution, separate selected and actual action, and preserve idempotency/uncertain results after crashes. Never mark success solely from a tool exit code, model confidence, or goal_reached prediction. Validate probabilities, option membership, state freshness, authority, input ownership and action scope before execution.

## Subsequent milestones

Add Bonsai structured recovery; deterministic browser tasks; Bonsai vision to structured scene to symbolic actions; full Agent Computer; then local UI with Pause/Stop/Take Control/Return Control/Approve/Reject/Resume. Human takeover must cancel queued agent input and require a fresh state/lease before resume.

Establish registries and immutable deployment identity early without implementing every feature. V0.1 uses base models. One active specialization LoRA at most; no stacking. Treat Bonsai LoRA conversion and hot-swap as compatibility experiments, not proven capabilities. Prefer Decider continued/delta training later. Do not silently train, change active deployments, download huge models, provision paid compute or upload private trajectories as a side effect of reading this prompt; use the task's actual authorization and scope.

Collect verified trajectories from day one. Build selection/redaction/normalization/labeling/dedup/group split/validation/manifest stages before training. The JSONL schemas are canonical AOS formats, not upstream trainer inputs: write tested converters. Keep examples synthetic and out of real training by default. Never use hidden chain-of-thought as the training record; use concise diagnoses, plans and verified outcomes.

Test state transitions, denied actions, cross-run persistence integrity, restart/reconciliation, stale UI elements, human takeover, invalid model output, timeouts, and regression cases as each feature lands. Grow end-to-end acceptance from file → shell → recovery → DOM → vision → LibreOffice PDF. Benchmark Decider-first against Bonsai-per-step fairly, recording verified success and resource cost. Promotion must be explicit, evidence-based and reversible with a known-good artifact/config.

Persist through the authorized implementation milestone; do not stop at a plan if the environment permits actual work. Report what changed, what was executed, what passed, what remains unverified, and the next concrete step. Keep STATUS.md current. Do not promise a flawless agent or claim unperformed tests.
