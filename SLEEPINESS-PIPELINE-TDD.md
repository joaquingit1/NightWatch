# Sleepiness Analysis Pipeline — Technical Design Document

Status: model-owner proposal; performance and venue robustness are not yet verified.
The runtime output contract, intervention gates, product language, and retention rules
are governed by `PRD.md` and `prds/00-OVERVIEW.md`. This document may choose the model
internals but must not bypass those interfaces or present target metrics as results.

Companion to `PRD.md` (section 7). Owner: Dev B (pipeline, models), Dev C (capture app, dataset ops). Everything here is additive or feature-flagged relative to the demo-critical path: the demo never depends on any model being good.

---

## 1. Design principles

1. **Regulator-grade metrics, not invented ones.** Every signal is a validated construct from driver-monitoring (DMS) literature: PERCLOS, blink duration, head-nod microsleeps. Judges can verify our claims in one search; we can defend every threshold.
2. **Explainable end to end.** The score always renders with its factor breakdown. Learned models augment the pipeline; they never turn it into a black box (feature importances, not opaque logits).
3. **One feature extractor, two consumers.** Training features and runtime features come from the identical code path (`FeatureExtractor` module). Any divergence silently invalidates the model.
4. **Confidence-gated output.** Below confidence threshold, the system asks for cooperation ("hold still, look at me") or abstains. A wrong number shown confidently is the worst possible failure.
5. **Geometry, not faces.** Default storage is derived landmarks/features. Raw video only with explicit extra consent. This is the privacy story, the PIPL defensibility, and what makes the dataset publishable.

## 2. Sensing geometry and deployment targets

Two camera sources, one code path (config flag):

| Source | FPS | Use | Notes |
|---|---|---|---|
| Go2 `color_image` via DimOS (WebRTC → laptop) | ~14 | Patrol-mode scoring, triage, nap passes | Low angle (shin height): pose correction mandatory; blink durations quantize to ~70 ms bins (fine for drowsy blinks ≥400 ms, coarse for normal ~150 ms) |
| Laptop webcam (booth) | 30 | Judge diagnosis, capture app sessions | Fine blink statistics; controlled framing |
| (Optional) RDK X5 saddle cam | 30 | Face-height perception at range | Track 21 garnish; same pipeline on RDK CPU/BPU publishing C1 over LAN |

Everything runs **natively on the laptop, Python, CPU**. Nothing runs on the dog (Go2 Air onboard compute is closed; DimOS assumes off-robot processing over WebRTC). MediaPipe Face Landmarker inference is tens of ms/frame on laptop CPU; end-to-end glass-to-decision latency ≈300-400 ms (WebRTC + inference), irrelevant for 60 s window decisions. Frames processed at native stream rate; rolling windows in a ring buffer inside a DimOS `Module`; outputs published as the C1 FatigueFrame stream + annotated frames to MJPEG.

## 3. Per-frame feature extraction

### 3.1 Face landmarks and head pose
MediaPipe Face Landmarker with `output_facial_transformation_matrixes=True`: 478 3D landmarks + a 4×4 head-pose matrix per frame. Also enable blendshapes (eyeBlinkLeft/Right) as an auxiliary channel.

### 3.2 Pose-corrected EAR (the core geometric nugget)
Naive EAR (Soukupová & Čech 2016): `EAR = (‖p2−p6‖ + ‖p3−p5‖) / (2·‖p1−p4‖)` on 2D projections. Failure mode: head pitch/yaw compresses projected distances, so looking down reads as eye closure. Our low camera angle makes this systematically worse than automotive.

Correction: apply the inverse facial transformation matrix to the 3D eye landmarks, rotating the face into a canonical frontal frame; compute EAR there. Result is invariant to head orientation. Then normalize per person: `EAR_n = EAR_c / EAR_open_baseline` (baseline from calibration, section 5).

Validation artifact (engineering wall): one subject sweeps their head with eyes open; plot naive EAR (swinging) vs corrected EAR (flat) on the same axes.

### 3.3 Eye-state CNN channel
A small CNN (≤1M params, MobileNet-scale or 5-layer) on per-eye crops (landmark-derived, ~24×24 to 64×64), output = P(closed). Pretrain on MRL Eye (~84k crops) + CEW (both small, public, downloadable via ModelScope/mirrors); optional fine-tune on venue eye crops. Purpose: robustness where geometric EAR is weakest (glasses, squints, low resolution). **Why eye-state and not a pretrained drowsiness model:** eye-state datasets are accessible where drowsiness video datasets are 100GB+/gated; eye crops transfer across domains where driver-cab models do not; the output is a per-frame *feature* that composes with our temporal stack instead of replacing it; it is auditable live (probability next to the crop); and it preserves "the drowsiness model is ours, trained here." Disagreement between EAR and CNN channels is itself a low-confidence signal.

### 3.4 Other per-frame features
- Head pose (pitch/yaw/roll) + angular velocities → nod-event detector (sharp pitch drop, slow recovery).
- MAR (mouth aspect ratio) → yawn events (>4 s spikes; supporting signal only).
- Body pose (MediaPipe Pose): neck-torso angle, forward-head, head-on-desk detector; movement entropy (fidget/agitation).
- Identity: DimOS `person_tracker` + `reid` for opted-in subjects (ledger continuity, subject IDs for the dataset).

### 3.5 Windowed features (60 s rolling, 1 s hop)
PERCLOS (fraction of window with closure ≥80%, from corrected EAR and from CNN channel separately); blink count; blink duration mean/p50/p90; long-closure (≥1 s microsleep) count; nod count; yawn count; EAR variance; pose stats; movement entropy; plus ledger-derived sedentary hours.

## 4. Scoring stack

```
FeatureExtractor (shared) ──► per-frame features ──► 60s windows
   ├── S1 Threshold rules (DMS literature): PERCLOS>0.15, blink>400ms, nods…  [shipped fallback]
   ├── S2 LightGBM on window statistics                                        [primary learned]
   └── S3 GRU/TCN (10k-100k params) on 10Hz feature sequences                  [comparison]
                     ▼
   Fusion + sanity band + confidence gate ──► RestScore (0-100, factors, confidence)
```

- **S1 thresholds** ship by default and remain the fallback forever. Weights fixed and interpretable.
- **S2 LightGBM** is the primary learned scorer once validated: most data-efficient at our n, trains in seconds, feature importances keep explainability.
- **S3 GRU** exists for the model-comparison chart and to capture dynamics window stats blur; heavy regularization + augmentation (temporal jitter, feature dropout, noise). If it loses to S2, that's a reported result, not a failure.
- **Sanity band:** if S2 diverges from S1 beyond a band, confidence drops and the UI shows reduced confidence rather than a confident outlier.
- **Feature flag:** `scorer = thresholds | lgbm | fused`, switchable live from the director console.
- Rejected up front: end-to-end video models (VideoMAE/3D-CNN on face crops). At 80-150 sessions they memorize identities and lighting, kill explainability, and are heavy at the edge. This is a deliberate, defended decision, not an omission.

## 5. Calibration and confidence

- **Quick-calibration (booth/judge mode):** population-default baselines, refined online over the first ~15 s (open-eye EAR percentile tracking). Score renders ≤20 s from face lock.
- **Full calibration (adopters):** 60-90 s capture session sets personal baselines (open-eye EAR, blink rate, resting pose), stored against reid identity.
- **Confidence inputs:** face size/resolution, landmark quality, pose extremity, channel agreement (EAR vs CNN), calibration state. Below gate: UI shows "calibrating", dog asks for cooperation, no number shown.

## 6. The venue dataset (nugget 3)

**Unit of collection: 60-90 s video session at 10-30 fps** (stills are useless: PERCLOS/blinks/nods are temporal). Capture app is embedded in the adoption flow (Dev C, day-one deliverable) and open to booth visitors.

**Per-session record (contract C6):** feature series (from the shared FeatureExtractor, computed at capture time), KSS self-rating (Karolinska Sleepiness Scale 1-9, the standard subjective instrument), hours-awake, glasses y/n, lighting tag, person_id, consent flags. Raw video kept only with explicit extra checkbox.

**Label mapping:** 3-class (alert 1-3, impaired 4-6, drowsy 7-9) for classification; raw KSS retained for regression/correlation.

**Collection plan exploits the venue's arc:** early days are alert-rich, the final night is a scheduled **drowsy harvest** (the venue is full of KSS-8 humans). Targets: ≥40 sessions day one, ≥40 day two, ≥40 final night; ≥120 total, ≥60 unique subjects, with repeat sessions per person across fatigue levels (within-person contrast is the highest-value data; the adopted pack provides it naturally).

**Deliverable:** anonymized feature dataset (no raw video, consented) published in the repo. The dataset is itself a judged artifact and the best single line of the pitch: "we built, labeled, and published a drowsiness dataset on the most sleep-deprived population on earth, live, at this hackathon."

## 7. Training and evaluation

- **Splits: subject-disjoint only** (leave-subjects-out CV). Random frame/session splits leak identity and inflate AUC; "our AUC is subject-disjoint" is the sentence that survives an ML judge.
- **Metrics:** ROC/AUC (3-class: one-vs-rest + macro), confusion matrix, and Spearman correlation of RestScore vs raw KSS.
- **Baselines compared:** S1 vs S2 vs S3 on identical splits. Honest reporting whatever the ranking.
- **Compute:** LightGBM trains on laptop in seconds; GRU in minutes; HyperAI 5090 perk used for the GRU hyperparameter sweep and eye-CNN pretraining overnight (night two). Final retrain on full dataset the evening of July 25; charts exported; model + dataset committed before the 1:00 AM deadline.
- **Class-imbalance handles:** if drowsy samples lag by day two, collapse to 2-class; within-person deltas as auxiliary target if needed.

## 8. Breathing verification at nap stops (adjacent module, same owner)

Dog stationary at 1-2 m from a still sleeper (ideal conditions for the technique): torso ROI from pose landmarks → optical-flow vertical displacement → band-pass 0.1-0.5 Hz → FFT peak = respiration rate, with amplitude/SNR confidence gate. Output: rate + live waveform to the booth screen and ledger. Abstains (confidence gate) rather than guesses when the subject moves or the ROI is occluded. This is the eldercare capability in miniature and a live WOW inside the existing nap beat.

## 9. Wall artifacts produced by this pipeline

1. Naive vs pose-corrected EAR head-sweep plot.
2. Subject-disjoint ROC/AUC: thresholds vs LightGBM vs GRU.
3. Feature-importance chart ("blink duration and nod count dominate, exactly as the DMS literature predicts").
4. RestScore vs KSS correlation scatter (n = venue subjects).
5. Live breathing waveform (screen, not print).
6. Latency budget panel (frame → landmarks → features → score → decision).

## 10. Risks specific to this pipeline

| Risk | Handle |
|---|---|
| Glasses/IR-coated lenses break EAR | Eye-CNN channel; confidence gate; disclose exclusions |
| Low angle defeats face mesh at range | Two-stage: posture triage far, face work near; 30fps webcam at booth |
| Too few drowsy labels | Final-night harvest; 2-class fallback; report n honestly |
| Learned model loses to thresholds | Ship thresholds; publish the comparison anyway (methodological maturity scores) |
| Feature drift between capture and runtime | Single shared FeatureExtractor, enforced by imports + a golden-vector unit test |
| Dataset download stalls (MRL/CEW) | Vendor via ModelScope/mirrors day one; eye-CNN is optional channel, not a dependency |
| PIPL/consent concerns | Geometry-only default, explicit checkboxes, deletion path, adopters-only identification |

## 11. Milestone gates (maps to PRD phases)

- **G1 (in P2):** FeatureExtractor + pose-corrected EAR + S1 live on webcam; capture app collecting; head-sweep plot produced.
- **G2 (in P4):** breathing verifier live at a staged nap; C1 stream consumed by policy and UI.
- **G3 (in P6):** ≥100 sessions; drowsy harvest done.
- **G4 (in P7):** S2/S3 trained + subject-disjoint validation; feature flag decision (in or honestly out); wall charts printed; dataset published.
