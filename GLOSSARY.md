# Glossary — Chinese terms in `data/`

The data files keep their Chinese keys and category names, and the code in
`code/` keeps its Chinese comments. Neither is translated in place, for two
different reasons.

**`data/` is byte-frozen.** Every file's sha256 is recorded in `MANIFEST.json`.
Renaming a key changes the checksum, and therefore changes the dataset the paper
cites. The freeze is the point; translation would break it.

**`code/` is released verbatim.** These are the scripts as they run in production.
A translated copy would be a different artefact from the one that produced the
numbers, and a reader would be right to ask which one was run.

So the terms are mapped here instead. `reproduce.py --lang en` prints its whole
report in English; this table is what you need for the data files themselves.

Stock short names are deliberately **not** listed: each row of the data carries the
stock code, which is the identifier that resolves unambiguously, and a table of
~5,200 invented English names would bury the 190 terms that matter.

## Structural terms

These appear as keys or as values of classification fields. You need them to read
the JSON at all.

| Chinese | English | What it marks |
| --- | --- | --- |
| `层` | layer | gate layer of a sector: bit or support |
| `用途` | use | which layer consumes this threshold |
| `标定` | calibration | how the threshold was calibrated, and its provenance |
| `全部` | all | whole-market scope, alongside technology / traditional |
| `科技` | technology | the technology industry set (37 industries) |
| `传统` | traditional | the traditional industry set |
| `核` | core | core ring of a sector zone |
| `核心` | core | same as 核, written out as a value |
| `中` | middle | middle ring of a sector zone |
| `中层` | middle layer | same as 中, written out as a value |
| `外` | periphery | outer ring of a sector zone |
| `外围` | periphery | same as 外, written out as a value |
| `信号链` | signal chain | the signal side of the Bit-Watt framework |
| `能量链` | energy chain | the energy side of the Bit-Watt framework |
| `信号×能量` | signal x energy | belongs to both chains |

## Industries (128)

The industry taxonomy is **self-built**, not Shenwan. Names follow the East Money
sector starting point; the mapping from stock to industry is in
`data/code_industry_paper.json`. Thirty-seven of these are classed as technology
industries (`data/bw_paper.json` → `tech_industries.list`); that split is what the
tearing measure `gap` is computed across.

| Chinese | English |
| --- | --- |
| `专业服务` | Professional services |
| `专用设备` | Special-purpose equipment |
| `中药` | Traditional Chinese medicine |
| `互联网服务` | Internet services |
| `交运物流` | Transport and logistics |
| `交运设备` | Transport equipment |
| `仪器仪表` | Instruments and meters |
| `保险` | Insurance |
| `光伏设备` | Photovoltaic equipment |
| `光学光电子` | Optics and optoelectronics |
| `光通信` | Optical communications |
| `公用事业` | Utilities |
| `农牧饲渔` | Agriculture, livestock, feed and fishery |
| `农药兽药` | Agrochemicals and veterinary drugs |
| `券商信托` | Brokerages and trusts |
| `包装材料` | Packaging materials |
| `化学制品` | Chemical products |
| `化学制药` | Chemical pharmaceuticals |
| `化学原料` | Basic chemical materials |
| `化工行业` | Chemicals |
| `化纤行业` | Chemical fibres |
| `化肥行业` | Fertilisers |
| `医疗器械` | Medical devices |
| `医疗服务` | Healthcare services |
| `医疗行业` | Healthcare |
| `医药制造` | Pharmaceutical manufacturing |
| `医药商业` | Pharmaceutical distribution |
| `医药生物` | Pharmaceuticals and biotech |
| `半导体` | Semiconductors |
| `商业百货` | Retail and department stores |
| `园林工程` | Landscaping |
| `国防军工` | Defence |
| `国际贸易` | International trade |
| `基础化工` | Basic chemicals |
| `塑料制品` | Plastic products |
| `塑胶制品` | Plastic and rubber products |
| `多元金融` | Diversified financials |
| `安防设备` | Security equipment |
| `家用轻工` | Household light industry |
| `家电行业` | Home appliances |
| `小金属` | Minor metals |
| `工程咨询服务` | Engineering consulting |
| `工程建设` | Construction and engineering |
| `工程机械` | Construction machinery |
| `工艺商品` | Craft goods |
| `房地产` | Real estate |
| `房地产开发` | Real estate development |
| `房地产服务` | Real estate services |
| `房地产综合服务` | Integrated real estate services |
| `教育` | Education |
| `文化传媒` | Media and culture |
| `文教休闲` | Education, culture and leisure goods |
| `旅游酒店` | Travel and hotels |
| `有色金属` | Non-ferrous metals |
| `木业家具` | Timber and furniture |
| `机械行业` | Machinery |
| `机械设备` | Machinery and equipment |
| `橡胶制品` | Rubber products |
| `民航机场` | Airlines and airports |
| `水泥建材` | Cement and building materials |
| `汽车` | Automobiles |
| `汽车整车` | Complete vehicles |
| `汽车服务` | Auto services |
| `汽车行业` | Automotive |
| `汽车零部件` | Auto parts |
| `消费电子` | Consumer electronics |
| `港口水运` | Ports and shipping |
| `游戏` | Games |
| `煤炭` | Coal |
| `煤炭行业` | Coal industry |
| `煤炭采选` | Coal mining and dressing |
| `燃气` | Gas utilities |
| `物流行业` | Logistics |
| `环保工程` | Environmental engineering |
| `环保行业` | Environmental protection |
| `玻璃玻纤` | Glass and fibreglass |
| `玻璃陶瓷` | Glass and ceramics |
| `珠宝首饰` | Jewellery |
| `生物制品` | Biologics |
| `电信运营` | Telecom operators |
| `电力行业` | Power |
| `电力设备` | Power equipment |
| `电子` | Electronics |
| `电子信息` | Electronic information |
| `电子元件` | Electronic components |
| `电子化学品` | Electronic chemicals |
| `电机` | Electric motors |
| `电池` | Batteries |
| `电源设备` | Power supply equipment |
| `电网设备` | Grid equipment |
| `石油石化` | Oil and petrochemicals |
| `石油行业` | Oil |
| `社会服务` | Social services |
| `纺织服装` | Textiles and apparel |
| `纺织服饰` | Textiles and apparel |
| `综合行业` | Conglomerates |
| `美容护理` | Beauty and personal care |
| `能源金属` | Energy metals |
| `航天航空` | Aerospace |
| `航空机场` | Airlines and airports |
| `航运港口` | Shipping and ports |
| `船舶制造` | Shipbuilding |
| `装修建材` | Decoration and building materials |
| `装修装饰` | Decoration and finishing |
| `计算机` | Computers |
| `计算机设备` | Computer hardware |
| `证券` | Securities |
| `贵金属` | Precious metals |
| `贸易行业` | Trading |
| `软件开发` | Software development |
| `软件服务` | Software services |
| `输配电气` | Transmission and distribution equipment |
| `通信` | Telecommunications |
| `通信服务` | Telecom services |
| `通信设备` | Telecom equipment |
| `通用设备` | General equipment |
| `通讯行业` | Telecommunications |
| `造纸印刷` | Paper and printing |
| `酿酒行业` | Brewing and distilling |
| `采掘行业` | Mining |
| `钢铁行业` | Steel |
| `铁路公路` | Rail and highways |
| `银行` | Banks |
| `非金属材料` | Non-metallic materials |
| `非金属材料Ⅱ` | Non-metallic materials II |
| `风电设备` | Wind power equipment |
| `食品饮料` | Food and beverages |
| `高速公路` | Expressways |

## Sector zones (62)

Zones are a second, orthogonal axis: a thematic grouping used by the Layer1 gate
(`data/bw_paper.json` → `sector_zones.map`, each zone tagged core / middle /
periphery). A stock's zone does not follow from its industry, and changing one
does not change the other.

| Chinese | English |
| --- | --- |
| `AI数据中心_储能_主动缓冲层` | AI data centre / storage / active buffer layer |
| `AI数据中心_电源_UPS_HVDC` | AI data centre / power / UPS / HVDC |
| `AI眼镜_智能硬件` | AI glasses / smart hardware |
| `AI算力硬件_Infra` | AI compute hardware / infrastructure |
| `MLCC_被动元器件` | MLCC / passive components |
| `PCB_AI服务器背板_NVLink` | PCB / AI server backplane / NVLink |
| `PCB材料_M9_Q布_石英电子布` | PCB materials / M9 / Q-glass / quartz electronic cloth |
| `SpaceX供应链(二级/三级)` | SpaceX supply chain (tier 2 / tier 3) |
| `中国商业航天供应链` | China commercial space supply chain |
| `人形机器人_FigureAI_宇树供应链` | Humanoid robots / Figure AI / Unitree supply chain |
| `人形机器人_Tesla_Optimus供应链` | Humanoid robots / Tesla Optimus supply chain |
| `低空飞行_eVTOL电池供应链` | Low-altitude flight / eVTOL battery supply chain |
| `充换电` | EV charging and battery swap |
| `光模块光互连_英伟达` | Optical modules and interconnect / NVIDIA |
| `军工主机_航发动力` | Defence primes / aero-engines |
| `军工主机_航空整机` | Defence primes / complete aircraft |
| `军工电子_航天零部件` | Defence electronics / space components |
| `功率半导体_电源管理IC` | Power semiconductors / power-management ICs |
| `半导体无尘屋_洁净室工程` | Semiconductor cleanrooms / cleanroom engineering |
| `半导体设备_刻蚀薄膜` | Semiconductor equipment / etch and thin film |
| `半导体设备_测试ATE` | Semiconductor equipment / test (ATE) |
| `卫星互联网_商业航天` | Satellite internet / commercial space |
| `变压器_固态变压器SST_数据中心电网` | Transformers / solid-state transformers (SST) / data-centre grid |
| `可控核聚变` | Controlled nuclear fusion |
| `商业航天_3D打印` | Commercial space / 3D printing |
| `商业航天_测控与地面站` | Commercial space / TT&C and ground stations |
| `固态电池` | Solid-state batteries |
| `地缘_油气_受益涨价周期` | Geopolitics / oil and gas / price-cycle beneficiaries |
| `地缘紧张_航运` | Geopolitical tension / shipping |
| `太空光伏` | Space photovoltaics |
| `存储芯片` | Memory chips |
| `工业母机` | Machine tools |
| `战略金属_钨钽` | Strategic metals / tungsten and tantalum |
| `战略金属_钴` | Strategic metals / cobalt |
| `战略金属_铝` | Strategic metals / aluminium |
| `战略金属_锡` | Strategic metals / tin |
| `有色金属_矿山设备` | Non-ferrous metals / mining equipment |
| `服务机器人_家庭场景_具身智能` | Service robots / home settings / embodied intelligence |
| `模拟芯片` | Analogue chips |
| `氢能_燃料电池汽车_绿色氨醇` | Hydrogen / fuel-cell vehicles / green ammonia and methanol |
| `汽车零部件_新能源` | Auto parts / new energy |
| `海洋经济_深海装备_海缆` | Marine economy / deep-sea equipment / submarine cable |
| `浸没液冷冷却液_材料` | Immersion cooling fluids / materials |
| `消费电子` | Consumer electronics |
| `液冷/热管理` | Liquid cooling / thermal management |
| `液冷_AI算力链_英伟达液冷` | Liquid cooling / AI compute chain / NVIDIA liquid cooling |
| `激光器芯片_InP_GaAs代工_光通讯` | Laser chips / InP and GaAs foundry / optical communications |
| `燃气轮机_北美AIDC主电源自建电源` | Gas turbines / North American AI data centre primary and self-built power |
| `特高压_光纤光缆` | UHV / optical fibre and cable |
| `特高压_智能电网` | UHV / smart grid |
| `环境工程` | Environmental engineering |
| `电信运营_算力基础设施` | Telecom operators / compute infrastructure |
| `电力设备_智能电网` | Power equipment / smart grid |
| `稀土永磁` | Rare-earth permanent magnets |
| `自动驾驶_英伟达DRIVE_激光雷达_Robotaxi` | Autonomous driving / NVIDIA DRIVE / lidar / robotaxi |
| `芯片EDA设计` | Chip EDA and design |
| `芯片设计_科创板` | Chip design / STAR Market |
| `虚拟电厂_智能电网_特高压` | Virtual power plants / smart grid / UHV |
| `超导` | Superconductors |
| `量子科技` | Quantum technology |
| `锂电池_动力电池` | Lithium batteries / EV batteries |
| `陕西商业航天集群` | Shaanxi commercial space cluster |

---

Generated by `gen_glossary.py`, which fails the build if any Chinese key or
category name in `data/` is missing from the table above. An incomplete glossary
is worse than none: the reader assumes the term they cannot find is unimportant.
