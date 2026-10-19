/**
 * 词性速查数据。
 *
 * 简称口径与本地词典（ECDICT）保持一致：词卡、生词本里显示的 `adj.` / `vt.`
 * 就是这里的 `abbr`，`a.`、`ad.` 这类旧写法放进 `aliases`，避免两处对不上。
 *
 * 纯静态数据：不请求接口，构建期打包，断网也能查。
 */

export interface PosExample {
  en: string
  zh: string
}

export interface PosEntry {
  /** 主简称：与本站词卡显示形态一致 */
  abbr: string
  /** 其他常见写法（旧式标注、带括号的标注等） */
  aliases?: string[]
  nameEn: string
  nameZh: string
  /** 一句话解释「它是什么」 */
  summary: string
  /** 使用要点：怎么用、和谁容易混 */
  usage: string
  example: PosExample
  /** 本站 ECDICT 词卡会显示这个简称 */
  inApp?: boolean
}

export interface PosGroup {
  key: string
  title: string
  subtitle: string
  intro: string
  entries: PosEntry[]
}

export interface SearchablePosEntry extends PosEntry {
  search: string
}

export interface SearchablePosGroup extends Omit<PosGroup, 'entries'> {
  entries: SearchablePosEntry[]
}

const GROUPS: PosGroup[] = [
  {
    key: 'core',
    title: '核心词性',
    subtitle: 'Parts of Speech',
    intro: '英语的十大词类，查词时最常遇到的就是这十个简称。',
    entries: [
      {
        abbr: 'n.',
        nameEn: 'noun',
        nameZh: '名词',
        summary: '表示人、事物、地点或抽象概念的词。',
        usage: '有可数与不可数之分；可数名词有单复数变化，作主语时谓语要跟它一致。',
        example: { en: 'The committee reached a decision after three hours.', zh: '委员会三小时后作出了决定。' },
        inApp: true,
      },
      {
        abbr: 'v.',
        nameEn: 'verb',
        nameZh: '动词',
        summary: '表示动作、状态或变化的词，是句子的核心。',
        usage: '有时态、语态和人称变化；按能否带宾语分及物（vt.）与不及物（vi.）。',
        example: { en: 'She writes every morning before breakfast.', zh: '她每天早餐前写作。' },
        inApp: true,
      },
      {
        abbr: 'adj.',
        aliases: ['a.'],
        nameEn: 'adjective',
        nameZh: '形容词',
        summary: '修饰名词或代词，说明性质、状态或特征。',
        usage: '可作定语（a quiet street）或表语（The street is quiet）；多数有比较级和最高级。',
        example: { en: 'They moved into a quiet street near the river.', zh: '他们搬到了河边一条安静的街上。' },
        inApp: true,
      },
      {
        abbr: 'adv.',
        aliases: ['ad.'],
        nameEn: 'adverb',
        nameZh: '副词',
        summary: '修饰动词、形容词、其他副词或整个句子。',
        usage: '说明方式、时间、地点、程度或频率；位置灵活，方式副词常放在动词后。',
        example: { en: 'He spoke softly so as not to wake the baby.', zh: '他轻声说话，以免吵醒孩子。' },
        inApp: true,
      },
      {
        abbr: 'pron.',
        nameEn: 'pronoun',
        nameZh: '代词',
        summary: '代替名词或名词短语，避免重复。',
        usage: '分人称、物主、反身、指示、疑问、不定和关系代词；要注意主格与宾格。',
        example: { en: 'Everyone brought something to the picnic.', zh: '每个人都带了点东西来野餐。' },
        inApp: true,
      },
      {
        abbr: 'prep.',
        nameEn: 'preposition',
        nameZh: '介词',
        summary: '放在名词或代词前，表示时间、地点、方向、原因等关系。',
        usage: '后接宾语构成介词短语；介词宾语用宾格（for me，而不是 for I）。',
        example: { en: 'The keys are on the table by the window.', zh: '钥匙在窗边的桌子上。' },
        inApp: true,
      },
      {
        abbr: 'conj.',
        nameEn: 'conjunction',
        nameZh: '连词',
        summary: '连接词、短语或句子的词。',
        usage: '并列连词（and / but / or）连接对等成分；从属连词（because / although / if）引导从句。',
        example: { en: 'She stayed home because it was raining.', zh: '因为下雨，她待在家里。' },
        inApp: true,
      },
      {
        abbr: 'int.',
        aliases: ['interj.'],
        nameEn: 'interjection',
        nameZh: '感叹词',
        summary: '独立表达情感、反应或招呼的词。',
        usage: '不充当句子成分，常单独成句，后面用逗号或感叹号。',
        example: { en: 'Ouch! That really hurt.', zh: '哎哟！真疼。' },
        inApp: true,
      },
      {
        abbr: 'art.',
        nameEn: 'article',
        nameZh: '冠词',
        summary: '放在名词前，说明是泛指还是特指。',
        usage: '只有 a / an / the 三个：不定冠词泛指「某一个」，定冠词特指「那个」。',
        example: { en: 'She bought a book, and the book turned out to be rare.', zh: '她买了一本书，结果那本书很稀有。' },
        inApp: true,
      },
      {
        abbr: 'num.',
        nameEn: 'numeral',
        nameZh: '数词',
        summary: '表示数量或顺序的词。',
        usage: '基数词（one, two）表数量，序数词（first, second）表顺序；作定语时通常不加复数。',
        example: { en: 'Three of the ten students finished first.', zh: '十名学生中有三名最先完成。' },
        inApp: true,
      },
      {
        abbr: 'det.',
        nameEn: 'determiner',
        nameZh: '限定词',
        summary: '放在名词前起限定作用：冠词、指示词、物主词、数量词都算。',
        usage: '回答「哪个 / 多少」；与形容词不同，限定词通常不能叠加（不说 the my book）。',
        example: { en: 'Those two ideas changed our whole plan.', zh: '那两个想法改变了我们的整个计划。' },
        inApp: true,
      },
    ],
  },
  {
    key: 'verb',
    title: '动词细分与形式',
    subtitle: 'Verbs & Forms',
    intro: '同一个动词有及物、不及物、助动词、分词等多种标注，别把它们当成不同词性。',
    entries: [
      {
        abbr: 'vt.',
        nameEn: 'transitive verb',
        nameZh: '及物动词',
        summary: '后面必须带宾语，意思才完整的动词。',
        usage: '可以构成被动语态；注意 raise（vt. 举起）与 rise（vi. 上升）这类成对词。',
        example: { en: 'She raised the window to let in some air.', zh: '她把窗户推上去，让空气进来。' },
        inApp: true,
      },
      {
        abbr: 'vi.',
        nameEn: 'intransitive verb',
        nameZh: '不及物动词',
        summary: '后面不直接跟宾语的动词。',
        usage: '常与介词搭配（listen to）；一般没有被动语态。',
        example: { en: 'The sun rose over the hills.', zh: '太阳从山丘上升起。' },
        inApp: true,
      },
      {
        abbr: 'aux.',
        nameEn: 'auxiliary verb',
        nameZh: '助动词',
        summary: '与主动词一起构成时态、语态、疑问或否定。',
        usage: 'be / do / have / will 等；助动词本身不承担主要词义。',
        example: { en: 'She has finished the report.', zh: '她已经完成了报告。' },
      },
      {
        abbr: 'modal',
        aliases: ['modal v.'],
        nameEn: 'modal verb',
        nameZh: '情态动词',
        summary: '表示能力、许可、义务、推测等语气的动词。',
        usage: 'can / could / may / might / must / should / would；后接动词原形，第三人称单数不加 -s。',
        example: { en: 'You must wear a helmet here.', zh: '这里你必须戴头盔。' },
      },
      {
        abbr: 'phr. v.',
        aliases: ['phr v', 'phrasal verb'],
        nameEn: 'phrasal verb',
        nameZh: '短语动词',
        summary: '动词加副词或介词，整体产生新的意义。',
        usage: '可分短语（turn the light off）宾语可插在中间；不可分短语（look after the child）不能拆。',
        example: { en: 'Please turn off the lights before you leave.', zh: '离开前请把灯关掉。' },
      },
      {
        abbr: 'inf.',
        nameEn: 'infinitive',
        nameZh: '不定式',
        summary: '动词原形，通常带 to，可作主语、宾语、定语或状语。',
        usage: '不能单独作谓语；使役动词和情态动词后接不带 to 的不定式（make him go）。',
        example: { en: 'To travel light makes the trip easier.', zh: '轻装旅行让旅程更轻松。' },
      },
      {
        abbr: 'ger.',
        nameEn: 'gerund',
        nameZh: '动名词',
        summary: '动词的 -ing 形式，起名词作用。',
        usage: '可作主语或宾语（enjoy reading）；形式与现在分词相同，但功能是名词性的。',
        example: { en: 'Swimming keeps him fit.', zh: '游泳让他保持健康。' },
      },
      {
        abbr: 'pp.',
        aliases: ['past part.'],
        nameEn: 'past participle',
        nameZh: '过去分词',
        summary: '动词的第三态，用来构成完成时和被动语态。',
        usage: '规则动词加 -ed，不规则动词要单独记（write → written）；也可作形容词。',
        example: { en: 'The broken vase was carefully wrapped.', zh: '碎掉的花瓶被小心包好。' },
      },
      {
        abbr: 'pres. p.',
        aliases: ['pres. part.'],
        nameEn: 'present participle',
        nameZh: '现在分词',
        summary: '动词的 -ing 形式，用来构成进行时或作分词短语。',
        usage: '表主动或进行（the rising sun）；与动名词同形，靠句法功能区分。',
        example: { en: 'Walking home, she noticed the new bookstore.', zh: '走回家时，她注意到了那家新书店。' },
      },
    ],
  },
  {
    key: 'noun',
    title: '名词与限定',
    subtitle: 'Nouns & Determiners',
    intro: '词典里常挤在词条开头的小字母，决定这个词能不能数、怎么变复数。',
    entries: [
      {
        abbr: 'C',
        aliases: ['[C]', 'countable'],
        nameEn: 'countable noun',
        nameZh: '可数名词',
        summary: '可以一个一个计数的名词，有单数和复数形式。',
        usage: '单数前一般要有限定词（a / the / my）；复数可以单独使用。',
        example: { en: 'There are three chairs in the room.', zh: '房间里有三把椅子。' },
      },
      {
        abbr: 'U',
        aliases: ['[U]', 'uncountable'],
        nameEn: 'uncountable noun',
        nameZh: '不可数名词',
        summary: '通常不按个数计、没有复数形式的名词。',
        usage: '谓语用单数；表数量要借助 a piece of、a bottle of 等量词。',
        example: { en: 'She gave me some useful advice.', zh: '她给了我一些有用的建议。' },
      },
      {
        abbr: 'pl.',
        nameEn: 'plural',
        nameZh: '复数',
        summary: '表示两个或两个以上，或标出名词的复数形式。',
        usage: '不规则复数要单独记（child → children）；词典标 pl. 也表示该词常以复数出现（scissors）。',
        example: { en: 'The children were playing in the yard.', zh: '孩子们在院子里玩。' },
        inApp: true,
      },
      {
        abbr: 'sing.',
        nameEn: 'singular',
        nameZh: '单数',
        summary: '表示一个，或标出名词的单数形式。',
        usage: '第三人称单数作主语时，一般现在时的动词要加 -s / -es。',
        example: { en: 'One child is waiting outside.', zh: '一个孩子在外面等着。' },
      },
      {
        abbr: 'poss.',
        aliases: ['possessive'],
        nameEn: 'possessive',
        nameZh: '所有格',
        summary: '表示所属关系的语法形式。',
        usage: "名词加 's 或改用 of 结构；my / your / his 等是形容词性物主代词。",
        example: { en: "The teacher's office is on the second floor.", zh: '老师的办公室在二楼。' },
      },
    ],
  },
  {
    key: 'form',
    title: '词形与其他标注',
    subtitle: 'Forms & Labels',
    intro: '词形变化、缩写、变体拼写等标注：它们描述「这个词长什么样」，不是新词类。',
    entries: [
      {
        abbr: 'compar.',
        aliases: ['comp.'],
        nameEn: 'comparative',
        nameZh: '比较级',
        summary: '表示「更……」，用于两者之间的比较。',
        usage: '短词加 -er（shorter），长词用 more（more difficult）；常与 than 搭配。',
        example: { en: 'This route is shorter than the one we took.', zh: '这条路线比我们走的那条短。' },
      },
      {
        abbr: 'superl.',
        aliases: ['sup.'],
        nameEn: 'superlative',
        nameZh: '最高级',
        summary: '表示「最……」，用于三者或以上的比较。',
        usage: '短词加 -est，长词用 most；作定语或表语时通常带 the。',
        example: { en: 'It was the most difficult question on the paper.', zh: '那是试卷上最难的一道题。' },
      },
      {
        abbr: 'abbr.',
        nameEn: 'abbreviation',
        nameZh: '缩写',
        summary: '单词或短语的缩略形式。',
        usage: '有的带点（Dr. / etc.），有的不带（NASA / UN）；正式写作首次出现时一般给全称。',
        example: { en: 'The UN was founded in 1945.', zh: '联合国成立于 1945 年。' },
        inApp: true,
      },
      {
        abbr: 'phr.',
        nameEn: 'phrase',
        nameZh: '短语',
        summary: '一组词构成一个意义单位，但不含完整的主谓结构。',
        usage: '常见类型有名词短语、动词短语、介词短语；词典用它标出固定搭配。',
        example: { en: 'In the long run, small habits matter more than big plans.', zh: '从长远看，小习惯比大计划更重要。' },
      },
      {
        abbr: 'suf.',
        aliases: ['suff.'],
        nameEn: 'suffix',
        nameZh: '后缀',
        summary: '加在词根后面构成新词或改变词性的成分。',
        usage: '-ness / -tion 常构成名词，-ful / -less 常构成形容词，-ly 常构成副词。',
        example: { en: 'Adding -ness to “kind” gives “kindness”.', zh: '给 kind 加上 -ness 就得到 kindness。' },
        inApp: true,
      },
      {
        abbr: 'AmE',
        aliases: ['US'],
        nameEn: 'American English',
        nameZh: '美式英语',
        summary: '美式拼写、用法或读音的标注。',
        usage: '如 color、center、traveled；本站音标里的「美」就是美音。',
        example: { en: 'American English usually spells “color” without a u.', zh: '美式英语中 color 通常不写 u。' },
      },
      {
        abbr: 'BrE',
        aliases: ['UK'],
        nameEn: 'British English',
        nameZh: '英式英语',
        summary: '英式拼写、用法或读音的标注。',
        usage: '如 colour、centre、travelled；本站音标里的「英」就是英音。',
        example: { en: 'British English writes “colour” with a u.', zh: '英式英语中 colour 带 u。' },
      },
    ],
  },
  {
    key: 'syntax',
    title: '句子成分',
    subtitle: 'Sentence Elements',
    intro: '这一组不是词性：它们描述一个词或短语在句子里「担任什么职务」，别和 n. / v. 混为一谈。',
    entries: [
      {
        abbr: 'S',
        nameEn: 'subject',
        nameZh: '主语',
        summary: '句子谈论的对象，回答「谁 / 什么」。',
        usage: '常由名词、代词、动名词或不定式充当。',
        example: { en: 'The old bridge collapsed.', zh: '那座老桥塌了。' },
      },
      {
        abbr: 'V',
        nameEn: 'verb / predicate',
        nameZh: '谓语（动词）',
        summary: '说明主语的动作或状态，是句子的核心。',
        usage: '一个句子通常只有一个谓语动词；其他动词形式要降级为非谓语。',
        example: { en: 'Birds sing at dawn.', zh: '鸟儿在黎明歌唱。' },
      },
      {
        abbr: 'O',
        nameEn: 'object',
        nameZh: '宾语',
        summary: '动作的承受者，回答「做什么 / 对谁」。',
        usage: '跟在及物动词或介词后面；人称代词作宾语要用宾格。',
        example: { en: 'She opened the window.', zh: '她打开了窗户。' },
      },
      {
        abbr: 'Oi',
        nameEn: 'indirect object',
        nameZh: '间接宾语',
        summary: '动作的接收者，通常指「给谁 / 为谁」。',
        usage: '常出现在 give / send / tell 等双宾语结构中，可改写为 to / for 短语。',
        example: { en: 'He gave me a map.', zh: '他给了我一张地图。' },
      },
      {
        abbr: 'Od',
        nameEn: 'direct object',
        nameZh: '直接宾语',
        summary: '动作直接作用的对象。',
        usage: '与间接宾语并存时，直接宾语通常指物；被动句中常被提到主语位置。',
        example: { en: 'He gave me a map.', zh: '他给了我一张地图。（a map 是 Od）' },
      },
      {
        abbr: 'C',
        aliases: ['Cs', 'Co'],
        nameEn: 'complement',
        nameZh: '补语',
        summary: '补充说明主语或宾语的身份、状态或特征。',
        usage: '说明主语的是主语补足语（She became a doctor），说明宾语的是宾语补足语。',
        example: { en: 'She became a doctor.', zh: '她成了一名医生。' },
      },
      {
        abbr: 'OC',
        nameEn: 'object complement',
        nameZh: '宾语补足语',
        summary: '补充说明宾语的状态或身份。',
        usage: '常见于 make / elect / call / keep 等动词后（elect her president）。',
        example: { en: 'They elected her president.', zh: '他们选她当主席。' },
      },
      {
        abbr: 'A',
        aliases: ['Adv.'],
        nameEn: 'adverbial',
        nameZh: '状语',
        summary: '修饰动词、形容词或整句，说明时间、地点、方式、原因等。',
        usage: '位置灵活：句首、句中、句尾都可能；不要把状语和 adv.（副词）混同。',
        example: { en: 'After the storm, we walked home slowly.', zh: '暴风雨后，我们慢慢走回家。' },
      },
      {
        abbr: 'Attr',
        nameEn: 'attributive',
        nameZh: '定语',
        summary: '修饰名词或代词，说明性质、特征或所属。',
        usage: '可由形容词、名词、介词短语或从句充当；单个形容词多放在名词前。',
        example: { en: 'The little girl with red shoes waved at us.', zh: '那个穿红鞋的小女孩朝我们挥手。' },
      },
    ],
  },
]

/** 检索串：简称、别名、中英文名、解释、用法、例句全部纳入 */
function searchText(e: PosEntry): string {
  return [
    e.abbr,
    ...(e.aliases ?? []),
    e.nameEn,
    e.nameZh,
    e.summary,
    e.usage,
    e.example.en,
    e.example.zh,
  ]
    .join(' ')
    .toLowerCase()
}

export const POS_GROUPS: SearchablePosGroup[] = GROUPS.map((g) => ({
  ...g,
  entries: g.entries.map((e) => ({ ...e, search: searchText(e) })),
}))

export const POS_TOTAL = POS_GROUPS.reduce((n, g) => n + g.entries.length, 0)

/** 本站词卡会显示的简称数量 */
export const POS_IN_APP = POS_GROUPS.reduce(
  (n, g) => n + g.entries.filter((e) => e.inApp).length,
  0,
)
