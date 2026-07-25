"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type LandingLanguage = "zh" | "en";

const LANGUAGE_STORAGE_KEY = "nightwatch.operator.language";

export const LANDING_COPY = {
  zh: {
    languageLabel: "语言",
    hero: {
      taglineLine1: "每个 AI 都想让你做得更多。",
      taglineLine2: "它却让你停下来。",
      subLine1: "当所有技术都在催你向前，它会提醒你休息。",
      subLine2: "自主巡逻 · 疲劳分诊 · 引导休息",
      reportNap: "登记小憩",
      watchPatrol: "观看巡逻",
      operatorConsole: "操作台",
    },
    story: {
      kicker: "项目理念",
      heading: "一只自主行动的情绪支持犬。",
      body: [
        "Unitree Go2 机器狗会在人们过度投入的地方自主巡逻——黑客松会场、凌晨两点的办公室、期末周的自习室。它通过摄像头识别疲劳迹象，给你的不是又一杯咖啡，而是一次休息邀请。如果你接受，它会带你前往休息区、守护小睡，并按时唤醒你。",
        "所有能力都在私有网络内的本地电脑上运行，现场演示不依赖云端。好奇是它的默认状态：在任何人发出指令之前，它就已经开始建图、巡逻与观察。",
      ],
    },
    timeline: {
      kicker: "你将看到",
      heading: "完整的物理闭环",
      moments: [
        {
          num: "01",
          title: "巡逻",
          body: "机器狗在已建图的走廊中自主移动。",
        },
        {
          num: "02",
          title: "分诊",
          body: "展位屏幕实时显示 0–100 的疲劳分数。",
        },
        {
          num: "03",
          title: "判断",
          body: "机器狗停下进行短暂观察，并解释检测到的信号。",
        },
        {
          num: "04",
          title: "建议",
          body: "它用温和的预录语音建议你小睡。",
        },
        {
          num: "05",
          title: "引导",
          body: "它以谨慎的速度带你前往休息区。",
        },
        {
          num: "06",
          title: "小睡记录",
          body: "到达后登记小睡，并启动可见的唤醒倒计时。",
        },
        {
          num: "07",
          title: "表情动作",
          body: "遮挡手势会排队触发安全动作，而不会取消导航。",
        },
        {
          num: "08",
          title: "姿态",
          body: "卧下会持续保持；站起会解除保持并安全恢复。",
        },
      ],
    },
    signals: {
      kicker: "如何识别疲劳",
      headingLine1: "不靠一次眨眼。",
      headingLine2: "而是一段时间内的多种信号。",
      items: [
        {
          title: "眼睛睁开程度",
          body: "结合 PERCLOS 与眨眼时长，并校正头部角度，避免侧视被误判为困倦。",
        },
        {
          title: "点头与打哈欠",
          body: "在滚动观察窗口中持续累积细微动作，而不是只看某一帧。",
        },
        {
          title: "塌肩姿态",
          body: "当身体姿态可见时分析颈部与躯干角度——姿势变化比单帧更能说明问题。",
        },
        {
          title: "静止程度",
          body: "一段时间内的运动模式会汇入 0–100 的 RestScore，并附带可信度。",
        },
      ],
      note: "可信度较低时，系统会选择克制，不会在摄像头看不清时武断地说你累了。它只提示“疲劳风险”，绝不作医疗诊断。",
    },
    tech: {
      kicker: "技术栈",
      items: [
        "Unitree Go2",
        "DimensionalOS",
        "YOLOv8-Face",
        "MediaPipe",
        "本地大模型",
        "FastAPI",
        "Next.js",
        "SQLite 记录簿",
      ],
    },
    footer: {
      headingLine1: "来看看完整闭环。",
      headingLine2: "我们会展示休息记录。",
      intakeForm: "登记小憩",
      boothConsole: "展位控制台",
      spatialMap: "空间地图",
      credit: "创作于 AdventureX 2026 · 主题：Reverse · #adventurex2026",
    },
  },
  en: {
    languageLabel: "Language",
    hero: {
      taglineLine1: "Every AI makes you work more.",
      taglineLine2: "This one makes you stop.",
      subLine1: "Every AI wants you to push harder. This one wants you to rest.",
      subLine2: "Autonomous patrol · fatigue triage · guided rest",
      reportNap: "Report a nap",
      watchPatrol: "Watch it patrol",
      operatorConsole: "Operator console",
    },
    story: {
      kicker: "THE IDEA",
      heading: "An emotional support, autonomous dog.",
      body: [
        "A Unitree Go2 robot dog roams wherever people push themselves too hard — a hackathon floor, an office at 2am, a study hall during finals — reads signs of fatigue from a camera, and offers rest instead of another caffeine hit. If you accept, it walks you to a nap zone, keeps watch, and wakes you on schedule.",
        "Everything runs locally on laptops over a private network. No cloud required for the live demo. Curiosity is the default state — mapping, patrolling, and observing happen before anyone asks the dog to do anything.",
      ],
    },
    timeline: {
      kicker: "WHAT YOU WILL SEE",
      heading: "The closed physical loop",
      moments: [
        {
          num: "01",
          title: "Patrol",
          body: "The dog moves through a mapped corridor on its own.",
        },
        {
          num: "02",
          title: "Triage",
          body: "A live fatigue score (0–100) appears on the booth screen.",
        },
        {
          num: "03",
          title: "Diagnose",
          body: "The dog pauses, takes a short reading, and explains what it sees.",
        },
        {
          num: "04",
          title: "Prescribe",
          body: "It recommends a nap in a warm, pre-recorded voice.",
        },
        {
          num: "05",
          title: "Escort",
          body: "It leads you to the mattress at a careful pace.",
        },
        {
          num: "06",
          title: "Nap ledger",
          body: "Arrival registers the nap and starts a visible wake-check countdown.",
        },
        {
          num: "07",
          title: "Expression",
          body: "A hand cover queues a safe dog gesture without cancelling navigation.",
        },
        {
          num: "08",
          title: "Posture",
          body: "Lie down holds indefinitely; Stand releases the hold and resumes safely.",
        },
      ],
    },
    signals: {
      kicker: "HOW FATIGUE IS DETECTED",
      headingLine1: "Not one blink.",
      headingLine2: "A rolling window of signals.",
      items: [
        {
          title: "Eye openness",
          body: "PERCLOS and blink duration, corrected for head angle so looking sideways does not fake drowsiness.",
        },
        {
          title: "Head nods & yawns",
          body: "Micro-movements that accumulate across a rolling observation window.",
        },
        {
          title: "Slump",
          body: "Neck-torso angle when body pose is visible — posture tells a story a single frame cannot.",
        },
        {
          title: "Stillness",
          body: "Movement patterns over time feed a RestScore from 0 to 100 with a confidence indicator.",
        },
      ],
      note: "Low confidence means the system holds back rather than calling you tired when the camera cannot see you clearly. It says “fatigue risk,” never a medical diagnosis.",
    },
    tech: {
      kicker: "TECHNOLOGY",
      items: [
        "Unitree Go2",
        "DimensionalOS",
        "YOLOv8-Face",
        "MediaPipe",
        "Local LLM",
        "FastAPI",
        "Next.js",
        "SQLite ledger",
      ],
    },
    footer: {
      headingLine1: "Ask for the live loop.",
      headingLine2: "We'll show you the ledger.",
      intakeForm: "Intake form",
      boothConsole: "Booth console",
      spatialMap: "Spatial map",
      credit: "Built at AdventureX 2026 · Theme: Reverse · #adventurex2026",
    },
  },
} as const;

type LandingCopy = (typeof LANDING_COPY)[LandingLanguage];

interface LandingLanguageContextValue {
  language: LandingLanguage;
  copy: LandingCopy;
  chooseLanguage: (language: LandingLanguage) => void;
}

const LandingLanguageContext = createContext<LandingLanguageContextValue | null>(null);

export function LandingLanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<LandingLanguage>("zh");

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
      if (stored === "en") setLanguage("en");
    } catch {
      // The landing page remains usable when storage is unavailable.
    }
  }, []);

  useEffect(() => {
    const syncLanguage = (event: StorageEvent) => {
      if (event.key !== LANGUAGE_STORAGE_KEY) return;
      setLanguage(event.newValue === "en" ? "en" : "zh");
    };
    window.addEventListener("storage", syncLanguage);
    return () => window.removeEventListener("storage", syncLanguage);
  }, []);

  useEffect(() => {
    const previousLanguage = document.documentElement.lang;
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    return () => {
      document.documentElement.lang = previousLanguage;
    };
  }, [language]);

  const value = useMemo<LandingLanguageContextValue>(
    () => ({
      language,
      copy: LANDING_COPY[language],
      chooseLanguage: (nextLanguage) => {
        setLanguage(nextLanguage);
        try {
          window.localStorage.setItem(LANGUAGE_STORAGE_KEY, nextLanguage);
        } catch {
          // Keep the in-memory choice when storage is unavailable.
        }
      },
    }),
    [language],
  );

  return (
    <LandingLanguageContext.Provider value={value}>
      {children}
    </LandingLanguageContext.Provider>
  );
}

export function useLandingLanguage() {
  const context = useContext(LandingLanguageContext);
  if (!context) {
    throw new Error("useLandingLanguage must be used inside LandingLanguageProvider");
  }
  return context;
}

export function LandingLanguageSwitch() {
  const { copy, language, chooseLanguage } = useLandingLanguage();

  return (
    <div
      className="landing-language-switch"
      role="group"
      aria-label={copy.languageLabel}
    >
      <button
        type="button"
        data-active={language === "zh"}
        aria-pressed={language === "zh"}
        onClick={() => chooseLanguage("zh")}
      >
        中文
      </button>
      <button
        type="button"
        data-active={language === "en"}
        aria-pressed={language === "en"}
        onClick={() => chooseLanguage("en")}
      >
        EN
      </button>
    </div>
  );
}
