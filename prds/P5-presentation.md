# P5 — Intervention, NFC Response, and Rest-Area Escort

Objective: complete one consented physical care loop without microphone dependence.

## Work

1. Implement the intervention FSM:
   `CANDIDATE → CONFIRMING → APPROACHING → OFFERING → ACCEPTED/DECLINED →
   ESCORTING → ARRIVED → RETURN_TO_CURIOSITY`.
2. Approach with a locked track and respectful standoff distance.
3. Add concise screen text and optional sound; measure the actual audio output device
   and venue audibility.
4. Put a static NFC tag and QR code on the dog, pointing to the current-intervention
   mobile page.
5. Add signed, expiring escort/remind/decline requests and stale-request rejection.
6. Prove the phone-to-robot transport on the intended network. Keep an operator approval
   fallback until it survives venue testing.
7. Navigate accepted users to the tagged `rest_area` at reduced speed.
8. Release the lease and resume curiosity after arrival, decline, timeout, failure, or
   manual cancellation.

## Acceptance

- Five staged candidates approach the correct target without identity switching.
- Five accept, five remind, and five decline submissions produce the correct transition.
- Expired, duplicate, wrong-robot, and no-active-target requests never move the dog.
- Five accepted escorts reach `rest_area`; manual input safely stops every run.
- All terminal states return to curiosity within one second.
- The full flow works with audio muted and no microphone.

## Safety

The robot offers help; it does not coerce, diagnose, touch, or block a person.
