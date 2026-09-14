#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行政岗对照语料生成器：300 份，按档位分四层（每层 75 份），档位写进 manifest 当"标准答案"。

为什么要分层：随机生成的语料只能看分布，不能回答"排得准不准"。按档位分层之后，
漏放率（合格层被自动淘汰的比例）、误放率（不匹配层被自动达标/放行的比例）才有分母可算。
档位：
  1 明显合格   —— 本科 + 行政/人事实习（有具体事务与数字）+ 学生组织职务 + 办公软件能落地
  2 边缘合格   —— 应届、无行政实习，只有校园活动组织 / 行政兼职；专业不对口但沾边
  3 明显不匹配 —— 技术/设计/工程背景，没有任何行政人事相关经历
  4 注水型     —— 术语齐全（"熟练使用Office""沟通协调能力强"），但无具体事例、无组织经历、无数据

档位**只写进 manifest.json**，绝不写进简历正文（否则等于把答案喂给模型）。

用法：python tools/gen_admin_corpus.py --out local/samples_admin --per-tier 75 --seed 20260914
"""
from __future__ import annotations

import argparse
import json
import os
import random

SCHOOLS = ["重庆文理学院", "湖南工商大学", "河南财经政法大学", "江西财经大学现代经济管理学院", "山东工商学院",
           "河北经贸大学", "广西民族大学", "云南民族大学", "吉林工商学院", "湖北经济学院", "安徽财经大学",
           "山西财经大学", "贵州财经大学", "内蒙古财经大学", "兰州财经大学", "哈尔滨商业大学"]
TECH_SCHOOLS = ["重庆电子工程职业学院", "深圳职业技术学院", "南京工业职业技术大学"]
MAJORS_OK = ["行政管理", "人力资源管理", "工商管理", "汉语言文学", "秘书学", "公共事业管理", "劳动与社会保障"]
MAJORS_EDGE = ["英语", "法学", "财务管理", "市场营销", "旅游管理", "小学教育", "新闻学"]
MAJORS_BAD = ["计算机科学与技术", "软件工程", "机械设计制造及其自动化", "电子信息工程", "视觉传达设计",
              "土木工程", "自动化", "通信工程"]
CITIES = ["重庆", "长沙", "郑州", "济南", "武汉", "成都", "西安", "合肥", "南昌", "贵阳"]
COMPANIES = ["重庆渝州实业有限公司", "长沙弘景科技有限公司", "郑州嘉合商贸有限公司", "武汉星辰教育咨询有限公司",
             "成都锦程物流有限公司", "西安汇通建筑工程有限公司", "合肥盛达医疗器械有限公司", "南昌睿信人力资源有限公司"]
CLUBS = ["校学生会办公室", "院学生会生活部", "青年志愿者协会", "大学生艺术团", "心理健康协会", "辩论队",
         "创业就业协会", "宿舍管理委员会"]
OFFICE_STRONG = ["Excel（VLOOKUP、数据透视表、条件格式）", "Word（公文与合同排版、目录与修订）",
                 "WPS 全套（表格函数、PPT 母版）", "Excel（数据透视表）+ 钉钉审批流"]
OFFICE_MID = ["Word、Excel 基本操作", "Office 日常使用", "WPS 基础操作"]
OFFICE_FUZZY = ["熟练使用 Office 办公软件", "精通 Word/Excel/PPT", "熟练操作各类办公软件", "Office 办公软件熟练"]


def _edu(rng, major, school, degree="本科", years=("2021.9-2025.6",)):
    courses = rng.sample(["管理学原理", "办公自动化", "公文写作", "人力资源管理", "社会调查方法",
                          "秘书实务", "统计学基础", "组织行为学"], 4)
    honors = rng.choice(["校级二等奖学金", "校级三等奖学金", "优秀学生干部", "无", "院级三好学生"])
    return (f"教育经历\n{years[0]}  {school}  {major}  {degree}\n"
            f"主修课程：{'、'.join(courses)}\n"
            f"荣誉奖励：{honors}\n")


def tier1(rng, i):                                       # 明显合格
    school, major = rng.choice(SCHOOLS), rng.choice(MAJORS_OK)
    c1, c2 = rng.sample(COMPANIES, 2)
    city = rng.choice(CITIES)
    club = rng.choice(CLUBS)
    duty = rng.sample(["会议安排与纪要整理（月均 12 场，纪要 2 小时内出稿）",
                       "员工入离职手续与人事档案归档（在职期间办理 40 余人）",
                       "考勤统计与社保公积金资料整理（每月 200 余人次，零差错）",
                       "办公用品采购与库存台账（月度盘点，把月耗材支出压降 12%）",
                       "来访接待与电话转接（日均 30 余次）",
                       "公司年会与团建的落地执行（统筹 3 场活动，覆盖 150 人）"], 3)
    return (_edu(rng, major, school)
            + f"实习经历\n2024.7-2024.12  {c1}（{city}）  行政助理实习生\n"
              + "；".join(duty) + "。\n"
            + f"2024.1-2024.3  {c2}  人事助理实习生\n"
              f"协助整理 3 个部门的岗位说明书与员工花名册，用 Excel 建立人员信息台账（含在职/离职状态与合同到期提醒）；"
              f"参与校招宣讲会现场支持，收取并初步筛简历 200 余份。\n"
            + f"\n校园经历\n2022.9-2024.6  {club}  干事 → 副部长\n"
              f"负责协会活动的物资与场地对接，统筹 6 场活动，累计参与 500 余人次；"
              f"建立协会的物资台账与报销流程文档，接手后报销退回率从 3 成降到几乎为零。\n"
            + f"\n相关技能\n办公软件：{rng.choice(OFFICE_STRONG)}\n"
              f"其他：熟悉钉钉/企业微信审批流，能独立起草通知与会议纪要；普通话二级甲等\n"
            + f"\n自我评价\n做事有条理，习惯用清单和台账推进事务；面对琐碎重复的工作能保持耐心，"
              f"实习期间经手的档案与考勤未出现差错；愿意在行政/人事方向长期做下去。\n")


def tier2(rng, i):                                       # 边缘合格
    school, major = rng.choice(SCHOOLS), rng.choice(MAJORS_EDGE)
    club = rng.choice(CLUBS)
    part = rng.choice([("2024.3-2024.6  校园快递驿站  值班与台账登记\n负责取件登记与月度盘点，"
                        "日均处理 200 余件；协助站长整理费用台账。\n"),
                       ("2023.7-2023.8  {c}  前台兼职\n负责来访登记与电话转接，协助整理会议室与办公用品。\n"),
                       ("2024.9-2024.12  {c}  实习（非行政岗）\n负责客户资料录入与合同扫描归档，"
                        "工作内容以录入为主，未接触考勤与人事流程。\n")])
    if "{c}" in part:
        part = part.replace("{c}", rng.choice(COMPANIES))
    return (_edu(rng, major, school)
            + "实习/兼职经历\n" + part
            + f"\n校园经历\n2022.9-2024.6  {club}  成员\n"
              f"参与协会日常值班与活动执行，做过物资搬运、签到表与场地布置；"
              f"活动组织以配合为主，未独立负责过项目。\n"
            + f"\n相关技能\n办公软件：{rng.choice(OFFICE_MID)}\n"
              f"其他：普通话二级乙等；能使用问卷星、腾讯文档协作\n"
            + "\n自我评价\n性格随和，愿意做支持性工作；行政方面的经验还比较零散，"
              "希望在工作中系统学习档案与考勤的规范做法。\n")


def tier3(rng, i):                                       # 明显不匹配
    school = rng.choice(SCHOOLS)
    major = rng.choice(MAJORS_BAD)
    c = rng.choice(COMPANIES)
    proj = rng.choice(["参与公司后台管理系统的接口开发，负责 3 个模块的 CRUD 与单元测试",
                       "负责电商详情页与活动海报的设计，使用 PS/AI 完成 60 余张物料",
                       "参与机械装配工艺文件的编制与车间现场问题记录",
                       "负责嵌入式设备的调试与现场联调，输出测试报告 10 份",
                       "参与办公楼水电改造的施工管理与进度跟进"])
    skill = rng.choice(["Java、MySQL、Git、Linux 基础", "Photoshop、Illustrator、C4D", "AutoCAD、SolidWorks",
                        "Python、Pandas、爬虫", "PLC 编程、电气原理图"])
    return (_edu(rng, major, school)
            + f"实习经历\n2024.7-2024.12  {c}  {'开发实习生' if 'Java' in skill or 'Python' in skill else '技术实习生'}\n"
              f"{proj}。团队 6 人，我在其中承担执行角色。\n"
            + f"\n项目经历\n2023.10-2024.5  课程/个人项目\n"
              f"使用 {skill.split('、')[0]} 完成一个功能模块的实现与调试，代码托管在 GitHub，"
              f"期间查阅文档解决依赖冲突问题。\n"
            + f"\n相关技能\n{skill}\n其他：英语四级已过；能扛压、学习能力强\n"
            + "\n自我评价\n偏技术方向，动手能力较强；对行政事务接触很少，"
              "但自认为沟通没问题、也能学。\n")


def tier4(rng, i):                                       # 注水型
    school, major = rng.choice(SCHOOLS), rng.choice(MAJORS_EDGE + MAJORS_OK)
    return (_edu(rng, major, school)
            + "实习经历\n2024.7-2024.9  某公司  行政助理（实习）\n"
              "熟练使用 Office 办公软件处理日常事务，沟通协调能力强，做事细致有耐心，"
              "获得同事一致认可；具备良好的团队协作精神与责任心，能快速适应新环境。\n"
            + "\n校园经历\n在校期间积极参加各类社团活动，担任过活动志愿者，"
              "培养了较强的组织协调与沟通表达能力；注重团队配合，乐于助人。\n"
            + f"\n相关技能\n{rng.choice(OFFICE_FUZZY)}\n"
              f"具备良好的文字表达能力与执行力；学习能力强，能承受一定工作压力\n"
            + "\n自我评价\n性格开朗，为人真诚，工作认真负责；热爱行政工作，"
              "相信自己能够胜任岗位要求，期待与公司共同成长。\n")


TIERS = {1: ("明显合格", tier1), 2: ("边缘合格", tier2), 3: ("明显不匹配", tier3), 4: ("注水型", tier4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="local/samples_admin")
    ap.add_argument("--per-tier", type=int, default=75)
    ap.add_argument("--seed", type=int, default=20260914)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    rng = random.Random(a.seed)
    manifest = []
    n = 0
    for tier, (tier_name, fn) in TIERS.items():
        for k in range(a.per_tier):
            n += 1
            text = fn(rng, n).strip() + "\n"
            name = f"admin_{n:03d}.txt"
            with open(os.path.join(a.out, name), "w", encoding="utf-8") as f:
                f.write(text)
            manifest.append({"id": n, "file": name, "tier": tier, "tier_name": tier_name,
                             "chars": len(text)})
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"已生成 {n} 份 → {a.out}（每层 {a.per_tier} 份）")
    for tier, (tier_name, _) in TIERS.items():
        same = [m for m in manifest if m["tier"] == tier]
        print(f"  第 {tier} 层 {tier_name}：{len(same)} 份 · 平均 {sum(m['chars'] for m in same) // len(same)} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
