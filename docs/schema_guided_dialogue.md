# Schema-Guided Dialogue（SGD）数据集说明

## 1. 它是什么

Schema-Guided Dialogue（SGD）是 Google 发布的英文多领域任务型对话数据集。每条数据记录一名用户和虚拟助手完成某项任务的过程，例如找餐馆、订酒店、查银行余额或查询航班。

它的核心设计是 **schema-guided（由接口说明引导）**：模型不只看到对话，还会看到服务/API 的接口 schema，包括：

- 服务名称和描述；
- 可执行的 intent（可近似理解为 API 方法）；
- 每个 intent 所需或可选的参数（slot）；
- 参数的自然语言含义、是否为枚举值，以及候选值；
- 查询或交易完成后可能返回的字段。

因此 SGD 重点考察的不是“记住某一套固定标签”，而是模型能否根据 schema 描述，在新服务上理解用户意图、填充参数、选择动作并组织回复。

本仓库的数据位于 `data/schema_guided_dialogue/`。本地版本的规模为：

| 切分 | 对话数 | 对话 JSON 文件数 |
| --- | ---: | ---: |
| `train` | 16,142 | 127 |
| `dev` | 2,482 | 20 |
| `test` | 4,201 | 34 |
| 合计 | 22,825 | 181 |

每个切分还带有一个 `schema.json`。数据集原始对话是英文，数据授权见 `data/schema_guided_dialogue/LICENSE.txt`（CC BY-SA 4.0）。

## 2. 先记住四层结构

读 SGD 时，可以只记住下面这棵树：

```text
schema.json                         这项服务“能做什么”
dialogues_001.json                  一批对话
  └── dialogue                      一条完整对话
        └── turns[]                 一轮轮说话
              └── frames[]          这一轮针对哪些服务的标注
```

### 2.1 Schema 字段：工具说明书

`schema.json` 不是某一条对话，而是服务/API 的说明书。以 `Restaurants_1` 为例：

| 字段 | 含义 | 例子 |
| --- | --- | --- |
| `service_name` | 服务名；对话 frame 中的 `service` 会引用它 | `Restaurants_1` |
| `description` | 服务能做什么 | 餐馆搜索和预订 |
| `slots` | 服务接受的参数定义 | `city`、`cuisine`、`party_size` |
| `slots[].name` | 参数名 | `city` |
| `slots[].description` | 参数的自然语言含义 | 餐馆所在城市 |
| `is_categorical` | 是否只能从固定候选中取值 | `party_size` 为 `true` |
| `possible_values` | 枚举参数的合法值；开放参数通常为空 | `1`、`2`、`3` ... |
| `intents` | 服务支持的任务/方法 | `FindRestaurants` |
| `intents[].required_slots` | 执行该任务必须提供的参数 | `cuisine`、`city` |
| `intents[].optional_slots` | 可选参数及默认或特殊值 | `price_range: dontcare` |
| `intents[].result_slots` | 查询/交易可能返回的字段 | `restaurant_name`、`phone_number` |

把它翻译成工具接口，大致就是：

```text
FindRestaurants(cuisine, city, price_range?)
ReserveRestaurant(restaurant_name, city, time, date?, party_size?)
```

其中 `intent` 是“要做哪件事”，`slot` 是“做这件事需要的参数”。

### 2.2 Dialogue 字段：一条对话记录

```json
{
  "dialogue_id": "1_00000",
  "services": ["Restaurants_1"],
  "turns": ["...按时间排列的轮次..."]
}
```

| 字段 | 含义 |
| --- | --- |
| `dialogue_id` | 这条对话的唯一 ID |
| `services` | 这条对话涉及的服务列表；多领域对话可能有多个服务 |
| `turns` | 按时间顺序排列的用户轮和系统轮 |

### 2.3 Turn 字段：一次说话

```json
{
  "speaker": "USER",
  "utterance": "I would like for it to be in San Jose.",
  "frames": ["...这轮的服务标注..."]
}
```

| 字段 | 含义 |
| --- | --- |
| `speaker` | 说话方：`USER` 或 `SYSTEM` |
| `utterance` | 实际说出的英文文本 |
| `frames` | 这句话对应的语义标注；一轮可能有多个 frame |

### 2.4 Frame 字段：这句话在任务中的含义

| 字段 | 含义 |
| --- | --- |
| `service` | 当前 frame 属于哪个服务，如 `Restaurants_1` |
| `actions` | 这句话执行的对话行为，如告知、追问、推荐 |
| `slots` | 非枚举 slot 在原句中的字符位置 |
| `state` | 用户轮结束后的累计状态 |
| `service_call` | 系统真正记录的调用方法和参数 |
| `service_results` | 调用返回的结构化结果列表 |

`actions` 中最重要的字段是：

```json
{"act": "INFORM", "slot": "city", "values": ["San Jose"]}
```

- `act`：动作类型；`INFORM` 表示提供信息，`REQUEST` 表示索要信息，`OFFER` 表示提供候选；
- `slot`：动作作用于哪个参数；
- `values`：用户说出或系统提供的值；
- `canonical_values`：把不同说法标准化后的值。

`state` 只在用户轮中出现，常见结构是：

```json
{
  "active_intent": "FindRestaurants",
  "requested_slots": [],
  "slot_values": {"city": ["San Jose"]}
}
```

- `active_intent`：当前正在完成的任务；
- `requested_slots`：用户本轮向系统询问的字段；
- `slot_values`：到当前为止已经收集到的参数。它是数组，因为同一参数可能有多个表达或多个值。

`slots` 是文本定位信息。例如：

```json
{"slot": "city", "start": 29, "exclusive_end": 37}
```

表示 `utterance[29:37]` 是该 slot 的原文值，即 `San Jose`。这里是**字符下标**，不是 token 下标。

## 3. 第一个完整小例子：一轮用户输入

下面是训练集真实样本中一轮的精简版：

```json
{
  "speaker": "USER",
  "utterance": "I would like for it to be in San Jose.",
  "frames": [{
    "service": "Restaurants_1",
    "actions": [{
      "act": "INFORM",
      "slot": "city",
      "values": ["San Jose"],
      "canonical_values": ["San Jose"]
    }],
    "slots": [{"slot": "city", "start": 29, "exclusive_end": 37}],
    "state": {
      "active_intent": "FindRestaurants",
      "requested_slots": [],
      "slot_values": {"city": ["San Jose"]}
    }
  }]
}
```

按字段读这条数据：

1. `speaker=USER`：这是用户说的话；
2. `utterance`：用户原话是“我希望它在 San Jose”；
3. `service=Restaurants_1`：这句话属于餐馆服务；
4. `act=INFORM`：用户正在提供信息；
5. `slot=city`、`values=["San Jose"]`：提供的信息是城市；
6. `slots=[29,37)`：`San Jose` 在原文中的字符位置；
7. `state.active_intent=FindRestaurants`：当前目标是找餐馆；
8. `state.slot_values`：目前只收集到了 `city`，还缺 schema 要求的 `cuisine`。

因此系统下一步合理的动作是：

```text
REQUEST(cuisine)
```

也就是继续问用户“想吃什么菜系”，而不是立即调用餐馆搜索服务。

## 4. 它和“工具调用轨迹”的关系

可以把一条 SGD 对话看作**带完整语义标注的离线 Agent 交互轨迹**：

```text
用户话语
  -> 提取意图和参数，更新对话状态
  -> 助手决定追问、确认、推荐或结束
  -> （参数足够时）记录服务调用
  -> 记录服务返回的结构化结果
  -> 助手将结果表达为自然语言
```

例如，餐馆检索过程可抽象为：

```text
USER:   想找一家餐馆
SYSTEM: 想在哪个城市？                    action = REQUEST(city)
USER:   在 San Jose，想吃美式餐厅
         state = { intent: FindRestaurants,
                   city: ["San Jose"], cuisine: ["American"] }
SYSTEM: 调用 FindRestaurants(city="San Jose", cuisine="American")
         service_results = [若干餐馆实体]
SYSTEM: 推荐其中一家餐馆                    action = OFFER(restaurant_name)
```

这和消息队列只有表面相似性：它们都含有按顺序排列的事件。但 SGD 是预先收集并标注好的**离线数据集**，不是 Kafka、RabbitMQ 等系统中的实时消息流。

也要注意，`service_call` 和 `service_results` 是数据中记录的模拟服务交互，不是可直接访问真实酒店、航班或银行的工具。数据集中没有标准强化学习的环境奖励 `reward`；若要用于 RL，需要自行定义成功、轮数、无效动作等奖励函数。

## 5. 文件布局

```text
data/schema_guided_dialogue/
├── train/
│   ├── dialogues_001.json ... dialogues_127.json
│   └── schema.json
├── dev/
│   ├── dialogues_001.json ... dialogues_020.json
│   └── schema.json
├── test/
│   ├── dialogues_001.json ... dialogues_034.json
│   └── schema.json
├── sgd_x/                         # schema 的语言改写鲁棒性扩展
├── README.md                      # 官方格式说明
└── LICENSE.txt
```

每个 `dialogues_*.json` 都是一个 JSON 数组，数组中每个元素为一条完整对话。不要把文件当成 JSONL 逐行读取。

## 6. 第二个完整例子：服务调用、返回结果与回复

同一对话接下来的系统 frame 记录了工具调用：

```json
{
  "speaker": "SYSTEM",
  "utterance": "I see that at 71 Saint Peter there is a good restaurant which is in San Jose.",
  "frames": [{
    "service": "Restaurants_1",
    "service_call": {
      "method": "FindRestaurants",
      "parameters": {
        "city": "San Jose",
        "cuisine": "American"
      }
    },
    "service_results": [
      {
        "restaurant_name": "71 Saint Peter",
        "city": "San Jose",
        "cuisine": "American",
        "price_range": "moderate",
        "phone_number": "408-971-8523",
        "street_address": "71 North San Pedro Street"
      },
      {"restaurant_name": "Bazille", "city": "San Jose", "...": "..."}
    ],
    "actions": [
      {"act": "OFFER", "slot": "restaurant_name", "values": ["71 Saint Peter"]},
      {"act": "OFFER", "slot": "city", "values": ["San Jose"]}
    ]
  }]
}
```

该 frame 说明系统完成了四件不同的事：

1. 根据 schema 把目标映射为 `FindRestaurants`；
2. 用累计状态构造参数；
3. 得到多条结构化查询结果；
4. 在自然语言中只挑选 `71 Saint Peter` 作为推荐项，并用 `OFFER` 进行标注。

这正是 SGD 对工具型 Agent 有用的原因：它不仅记录“最终说了什么”，也记录了话语对应的状态、行为、调用和返回结果。

## 7. 可以怎样使用

### 对话状态跟踪（DST）

输入历史对话与当前用户话语，预测当前用户 frame 的 `active_intent`、`requested_slots` 和 `slot_values`。这是最常见的 SGD 任务。

```text
输入:  历史 + "I would like for it to be in San Jose."
输出:  FindRestaurants, {city: ["San Jose"]}
```

### 工具调用预测

在必填参数齐全后，预测系统的 `service_call.method` 与 `parameters`。

```text
输入: schema + 当前状态 {city: San Jose, cuisine: American}
输出: FindRestaurants(city="San Jose", cuisine="American")
```

### 对话策略或行为克隆

预测系统下一步 `actions`，例如应继续 `REQUEST(date)`，还是 `OFFER(restaurant_name)`、`CONFIRM` 或 `NOTIFY_SUCCESS`。如果将其用于 RL，可把数据中的系统动作视为离线示范，但奖励需要在训练环境或任务评测中额外构造。

### 回复生成

以对话上下文、预测或真实系统动作、`service_results` 为条件，生成系统 `utterance`。评测时必须避免把整条真实系统 frame（特别是答案文本）直接输入模型，否则会泄漏标签。

## 8. 读取数据的最小示例

```python
import json
from pathlib import Path

data_path = Path("data/schema_guided_dialogue/train/dialogues_001.json")
schema_path = Path("data/schema_guided_dialogue/train/schema.json")

with data_path.open() as file:
    dialogues = json.load(file)
with schema_path.open() as file:
    schemas = {item["service_name"]: item for item in json.load(file)}

dialogue = dialogues[0]
print(dialogue["dialogue_id"])
print(schemas["Restaurants_1"]["intents"])

for turn in dialogue["turns"]:
    print(turn["speaker"], turn["utterance"])
    for frame in turn["frames"]:
        if "state" in frame:
            print("  state:", frame["state"])
        if "service_call" in frame:
            print("  call:", frame["service_call"])
            print("  results:", frame["service_results"])
```

命令行快速查看一条样本：

```bash
jq '.[0]' data/schema_guided_dialogue/train/dialogues_001.json
```

## 9. 使用时的注意事项

- schema、服务名称和样本范围应以当前 split 中的 `schema.json` 为准；不能假设训练中出现过评测服务。
- 多领域对话的一轮可能包含多个 `frames`，不能只读取 `frames[0]`。
- `slots` 是字符位置，不是 token 位置；切分文本时使用 Python 字符串下标。
- 对话中的服务调用和结果是标注数据的一部分。离线训练可以使用它们；部署到真实系统时，必须将预测出的调用连接到真实、受权限和参数校验保护的工具。
- 做回复生成或策略评测时，按时间切分输入和监督目标，避免读入当前或未来系统轮的 `actions`、`service_call`、`service_results`、`utterance` 而造成标签泄漏。

## 10. 参考

- 本地官方说明：`data/schema_guided_dialogue/README.md`
- 数据集论文：Rastogi et al., *Towards Scalable Multi-domain Conversational Agents: The Schema-Guided Dialogue Dataset*, AAAI 2020。
- SGD-X：为 schema 提供多种语义等价的语言改写，用于测试模型是否对 schema 措辞鲁棒；文件在 `data/schema_guided_dialogue/sgd_x/`。
