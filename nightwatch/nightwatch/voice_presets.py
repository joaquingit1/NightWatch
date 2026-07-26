"""Canned voice lines shared by the operator console and the TTS cache.

The operator console's voice buttons play fixed lines, so SpeakSkill can
synthesize them once and keep the wav files on disk. Both sides import from
here so the button text and the prewarmed audio can never drift apart.
"""

SPEAK_GREETING = "你好，我是守夜犬。 Hello, I'm Night Watch."
SPEAK_INVITE = (
    "你看起来有点累，需要我带你去休息区吗？ "
    "You look tired. Would you like me to guide you to the rest area?"
)
SPEAK_PRESCRIBE = (
    "建议你去休息区睡一觉。愿意的话，扫一下我身上的二维码。 "
    "A short nap would help. Scan the QR code on my back if you'd like."
)
SPEAK_ESCORT_START = (
    "跟我来，我带你去休息区。 Follow me, I'll take you to the rest area."
)
SPEAK_ARRIVAL = "我们到了，好好休息。 Here we are. Rest well."
SPEAK_FAREWELL = (
    "别太累了，记得休息。再见！ Please remember to rest. Goodbye!"
)

# Confirmation lines the policy server speaks when the operator confirms a
# questionnaire submission. They MUST stay byte-identical to
# MANUAL_ESCORT_CONFIRMATION / NO_ESCORT_ACK in
# server/app/services/robot_bridge.py, or the prewarmed cache misses.
ESCORT_CONFIRMATION = (
    "收到，我带你去休息区，跟我来。 "
    "Got it — follow me, I'll take you to the rest area."
)
NO_ESCORT_ACK = (
    "收到，那我不打扰你了。记得好好休息，需要我时再扫我身上的二维码。 "
    "Got it, I will not keep you. Remember to rest, and scan my QR code "
    "if you need me."
)

# Every line SpeakSkill pre-synthesizes into its wav cache at warmup.
PRESET_LINES: tuple[str, ...] = (
    SPEAK_GREETING,
    SPEAK_INVITE,
    SPEAK_PRESCRIBE,
    SPEAK_ESCORT_START,
    SPEAK_ARRIVAL,
    SPEAK_FAREWELL,
    ESCORT_CONFIRMATION,
    NO_ESCORT_ACK,
)
