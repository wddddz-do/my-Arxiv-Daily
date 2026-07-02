#!/usr/bin/env python3
"""
ArXiv Paper Fetcher
自动爬取 arxiv 论文并生成 Markdown 报告

主要功能：
1. 按 arXiv 分类爬取论文；
2. 用 keywords 做初步筛选；
3. 用 topic_groups 把论文分到自定义主题下面；
4. 按日期生成 Markdown 文件；
5. 生成结果可用于 VitePress / GitHub Pages 展示。
"""

import os
import yaml
import arxiv
import time
import re
from datetime import datetime, timedelta, timezone
from arxiv import HTTPError


# ArXiv 分类名称映射
CATEGORY_NAMES = {
    # AI & Machine Learning
    "cs.AI": "Artificial Intelligence",
    "cs.CL": "Computation and Language",
    "cs.CV": "Computer Vision and Pattern Recognition",
    "cs.LG": "Machine Learning",
    "cs.NE": "Neural and Evolutionary Computing",

    # Architecture & Systems
    "cs.AR": "Architecture",
    "cs.DC": "Distributed, Parallel, and Cluster Computing",
    "cs.NI": "Networking and Internet Architecture",
    "cs.OS": "Operating Systems",
    "cs.PF": "Performance",
    "cs.DS": "Data Structures and Algorithms",

    # Software Engineering & Languages
    "cs.SE": "Software Engineering",
    "cs.PL": "Programming Languages",
    "cs.FL": "Formal Languages and Automata Theory",
    "cs.LO": "Logic in Computer Science",

    # Security & Cryptography
    "cs.CR": "Cryptography and Security",

    # Databases & Information
    "cs.DB": "Databases",
    "cs.IR": "Information Retrieval",
    "cs.DL": "Digital Libraries",
    "cs.SI": "Social and Information Networks",

    # Graphics & Multimedia
    "cs.GR": "Graphics",
    "cs.MM": "Multimedia",
    "cs.HC": "Human-Computer Interaction",

    # Theory & Math
    "cs.CC": "Computational Complexity",
    "cs.CY": "Computers and Society",
    "cs.DM": "Discrete Mathematics",
    "cs.GT": "Computer Science and Game Theory",
    "cs.IT": "Information Theory",
    "cs.SC": "Symbolic Computation",
    "cs.NA": "Numerical Analysis",

    # Robotics & Control
    "cs.RO": "Robotics",
    "cs.MS": "Mathematical Software",
    "cs.SY": "Systems and Control",
    "eess.SY": "Systems and Control",

    # Other
    "cs.GL": "General Literature",
    "cs.ET": "Emerging Technologies",
    "cs.MA": "Multiagent Systems",
    "cs.OH": "Other Computer Science",
    "cs.SD": "Sound",
}


# ArXiv API 使用条款：
# 每次请求之间至少间隔 3 秒。这里使用 10 秒，更保守，减少 429 风险。
ARXIV_API_DELAY = 10.0
ARXIV_MAX_RETRIES = 5
ARXIV_RETRY_BACKOFF = 30.0


def load_config(config_path):
    """加载 YAML 配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_existing_papers(docs_dir):
    """从 docs 文件夹中提取已经存在的 arXiv 论文 ID，避免重复写入"""
    existing_ids = set()
    pattern = r"arxiv\.org/abs/(\d+\.\d+)"

    if not os.path.exists(docs_dir):
        return existing_ids

    for root, _, files in os.walk(docs_dir):
        for file in files:
            if not file.endswith(".md"):
                continue

            file_path = os.path.join(root, file)

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    matches = re.findall(pattern, content)
                    existing_ids.update(matches)
            except Exception as e:
                print(f"警告: 读取文件 {file_path} 时出错: {e}")

    return existing_ids


def filter_by_keywords(paper, keywords):
    """根据 keywords 筛选论文。只要标题或摘要命中任一关键词，就保留。"""
    if not keywords:
        return True

    title_lower = paper.title.lower()
    summary_lower = paper.summary.lower()

    for keyword in keywords:
        keyword_lower = str(keyword).lower()
        if keyword_lower in title_lower or keyword_lower in summary_lower:
            return True

    return False


def get_topic_for_paper(paper, topic_groups):
    """
    根据论文标题和摘要，把论文分到自定义主题。

    例如：
    topic_groups:
      "机器狗 / 四足机器人":
        - quadruped robot
        - quadrupedal locomotion

    如果论文标题或摘要里出现这些关键词，就归到该主题下面。
    """
    if not topic_groups:
        return "其他相关论文"

    text = f"{paper.title} {paper.summary}".lower()

    for topic_name, topic_keywords in topic_groups.items():
        for keyword in topic_keywords:
            keyword_lower = str(keyword).lower()
            if keyword_lower in text:
                return topic_name

    return "其他相关论文"


def escape_html_in_md(text):
    """
    转义 Markdown 里的 < 和 >。

    VitePress 会把 Markdown 当成 Vue 模板处理。
    如果标题或摘要里有 <...>，可能被误认为 HTML 标签，导致构建失败。
    """
    text = text.replace("<", "\\<")
    text = text.replace(">", "\\>")
    return text


def truncate_summary(summary, max_length):
    """截断摘要到指定长度"""
    if len(summary) <= max_length:
        return summary

    truncated = summary[:max_length]
    last_period = truncated.rfind(".")

    if last_period > max_length * 0.7:
        return truncated[:last_period + 1]

    return truncated[:max_length] + "..."


def fetch_papers_by_category(client, category, start_date, end_date, max_results, keywords):
    """
    获取指定 arXiv 分类下的论文。

    返回：
    papers: 命中 keywords 的论文列表
    latest_published: 本分类中最新论文的提交时间
    """
    print(f"正在爬取分类: {category}")

    query = f"cat:{category}"

    # ArXiv API 的日期查询使用 YYYYMMDD 格式。
    # end_date 加一天，是为了包含 end_date 当天的所有论文。
    end_date_plus_one = end_date + timedelta(days=1)
    date_query = (
        f"submittedDate:[{start_date.strftime('%Y%m%d')} "
        f"TO {end_date_plus_one.strftime('%Y%m%d')}]"
    )

    search = arxiv.Search(
        query=f"{query} AND {date_query}",
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    papers = []
    latest_published = None

    retry_delay = ARXIV_RETRY_BACKOFF

    for attempt in range(ARXIV_MAX_RETRIES):
        try:
            for result in client.results(search):
                if filter_by_keywords(result, keywords):
                    papers.append(result)

                    if latest_published is None or result.published > latest_published:
                        latest_published = result.published

            break

        except HTTPError as e:
            if e.status == 429:
                if attempt < ARXIV_MAX_RETRIES - 1:
                    print(
                        f"  警告: 收到 429，请求过多。"
                        f"第 {attempt + 1}/{ARXIV_MAX_RETRIES} 次重试，"
                        f"等待 {retry_delay:.0f} 秒..."
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    print(f"  错误: 爬取 {category} 多次收到 429，跳过此分类")
            else:
                print(f"  警告: 爬取 {category} 时出错: HTTP {e.status} - {e}")
                break

        except Exception as e:
            print(f"  警告: 爬取 {category} 时出错: {e}")
            break

    print(f"  找到 {len(papers)} 篇论文")
    return papers, latest_published


def generate_markdown(papers_by_category, config, existing_ids=None, last_update=None):
    """
    生成 Markdown 数据结构。

    旧逻辑：
        日期 -> arXiv 分类 -> 论文
        例如：2026-07-01 -> cs.RO -> paper

    新逻辑：
        日期 -> 自定义主题 -> 论文
        例如：2026-07-01 -> 机器狗 / 四足机器人 -> (cs.RO, paper)

    这样页面标题会显示：
        机器狗 / 四足机器人
        空中机械臂
        无人机规划与控制
        无人机学习 / UAV Learning

    而不是：
        cs.RO
        cs.SY
        cs.LG
    """
    if existing_ids is None:
        existing_ids = set()

    topic_groups = config.get("topic_groups", {})

    papers_by_date = {}
    total_new = 0

    for category, papers in papers_by_category.items():
        if not papers:
            continue

        # 过滤已经存在的论文
        new_papers = [p for p in papers if p.get_short_id() not in existing_ids]

        if not new_papers:
            continue

        total_new += len(new_papers)

        for paper in new_papers:
            date = paper.published.strftime("%Y-%m-%d")
            topic = get_topic_for_paper(paper, topic_groups)

            if date not in papers_by_date:
                papers_by_date[date] = {}

            if topic not in papers_by_date[date]:
                papers_by_date[date][topic] = []

            # 保留 category，是为了表格里还能显示 arXiv 来源分类
            papers_by_date[date][topic].append((category, paper))

    return papers_by_date, total_new


def create_month_index(month_dir):
    """为每个月份目录创建 index.md"""
    date_files = []

    for filename in sorted(os.listdir(month_dir)):
        if filename.endswith(".md") and filename != "index.md":
            date_part = filename[:-3]
            date_files.append(date_part)

    if not date_files:
        return False

    lines = []
    month_name = os.path.basename(month_dir)

    lines.append(f"# {month_name}\n")
    lines.append("")
    lines.append("## 日期列表\n")
    lines.append("")

    for date in sorted(date_files, reverse=True):
        lines.append(f"- [{date}](./{date})")

    index_path = os.path.join(month_dir, "index.md")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return True


def unpack_paper_item(item):
    """
    兼容两种格式：
    1. 新格式：(category, paper)
    2. 旧格式：paper

    正常情况下，新代码会使用第一种。
    保留这个函数是为了避免旧数据结构导致程序崩掉。
    """
    if isinstance(item, tuple) and len(item) == 2:
        return item[0], item[1]

    category = getattr(item, "primary_category", "Unknown")
    return category, item


def topic_sort_key(topic, topic_groups):
    """
    控制主题显示顺序。

    如果 topic 在 config 里的 topic_groups 中，就按配置文件顺序显示。
    如果不在，就放到最后。
    """
    topic_order = list(topic_groups.keys())

    if topic in topic_order:
        return topic_order.index(topic)

    return len(topic_order)


def update_markdown_files_by_date(papers_by_date, config):
    """按日期生成 Markdown 文件，并按自定义主题分组"""
    docs_dir = config["output"]["dir"]
    include_summary = config["output"]["include_summary"]
    summary_max_length = config["output"]["summary_max_length"]
    cat_heading = "#" * config["markdown"]["category_heading_level"]
    topic_groups = config.get("topic_groups", {})

    if not os.path.exists(docs_dir):
        os.makedirs(docs_dir)

    total_files_created = 0
    total_papers = 0
    month_dirs = set()

    for date in sorted(papers_by_date.keys(), reverse=True):
        date_str = date
        papers_by_topic = papers_by_date[date]

        date_parts = date_str.split("-")
        year_month = f"{date_parts[0]}-{date_parts[1]}"
        year_month_dir = os.path.join(docs_dir, year_month)
        month_dirs.add(year_month_dir)

        if not os.path.exists(year_month_dir):
            os.makedirs(year_month_dir)

        lines = []
        lines.append(f"# {date_str}")
        lines.append("")

        sorted_topics = sorted(
            papers_by_topic.keys(),
            key=lambda t: topic_sort_key(t, topic_groups),
        )

        for topic in sorted_topics:
            paper_items = papers_by_topic[topic]

            if not paper_items:
                continue

            total_papers += len(paper_items)

            lines.append(f"{cat_heading} {topic}")
            lines.append("")

            if include_summary:
                lines.append("| 标题 | arXiv分类 | 作者 | 发布日期 | PDF | 摘要 |")
                lines.append("|------|-----------|------|----------|-----|------|")

                for item in paper_items:
                    category, paper = unpack_paper_item(item)

                    title = escape_html_in_md(paper.title)
                    title_link = f"[{title}](https://arxiv.org/abs/{paper.get_short_id()})"

                    category_name = CATEGORY_NAMES.get(category, category)
                    category_text = f"{category} - {category_name}"

                    authors = ", ".join([str(author) for author in paper.authors])
                    published_date = paper.published.strftime("%Y-%m-%d")
                    pdf_link = f"[下载](https://arxiv.org/pdf/{paper.get_short_id()}.pdf)"

                    summary = truncate_summary(
                        escape_html_in_md(
                            paper.summary
                            .replace("\n", " ")
                            .replace("|", "\\|")
                            .replace("\r", "")
                        ),
                        summary_max_length,
                    )

                    lines.append(
                        f"| {title_link} | {category_text} | {authors} | "
                        f"{published_date} | {pdf_link} | {summary} |"
                    )

            else:
                lines.append("| 标题 | arXiv分类 | 作者 | 发布日期 | PDF |")
                lines.append("|------|-----------|------|----------|-----|")

                for item in paper_items:
                    category, paper = unpack_paper_item(item)

                    title = escape_html_in_md(paper.title)
                    title_link = f"[{title}](https://arxiv.org/abs/{paper.get_short_id()})"

                    category_name = CATEGORY_NAMES.get(category, category)
                    category_text = f"{category} - {category_name}"

                    authors = ", ".join([str(author) for author in paper.authors])
                    published_date = paper.published.strftime("%Y-%m-%d")
                    pdf_link = f"[下载](https://arxiv.org/pdf/{paper.get_short_id()}.pdf)"

                    lines.append(
                        f"| {title_link} | {category_text} | {authors} | "
                        f"{published_date} | {pdf_link} |"
                    )

            lines.append("")

        file_path = os.path.join(year_month_dir, f"{date_str}.md")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        total_files_created += 1
        print(f" 创建文件: {file_path} ({len(papers_by_topic)} 个主题)")

    for month_dir in month_dirs:
        if create_month_index(month_dir):
            print(f" 创建索引文件: {os.path.join(month_dir, 'index.md')}")

    return total_files_created, total_papers


def get_state_file_path(docs_dir):
    """获取状态文件路径"""
    return os.path.join(docs_dir, ".state")


def load_state(state_file):
    """加载状态文件"""
    if not os.path.exists(state_file):
        return None

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def save_state(state_file, latest_paper_date):
    """保存状态文件：记录最新论文日期"""
    paper_date = latest_paper_date.strftime("%Y-%m-%d")

    state_dir = os.path.dirname(state_file)
    if state_dir and not os.path.exists(state_dir):
        os.makedirs(state_dir)

    with open(state_file, "w", encoding="utf-8") as f:
        yaml.dump({"last_run_date": paper_date}, f)


def save_last_update_time(update_time):
    """保存最后更新时间到 docs/last_update.yml"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    docs_dir = os.path.join(script_dir, "..", "docs")
    update_file = os.path.join(docs_dir, "last_update.yml")

    os.makedirs(docs_dir, exist_ok=True)

    with open(update_file, "w", encoding="utf-8") as f:
        f.write(f"last_update: {update_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
        f.write(f"last_update_date: {update_time.strftime('%Y-%m-%d')}\n")
        f.write(f"last_update_time: {update_time.strftime('%H:%M')}\n")


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "config", "arxiv_config.yaml")

    config = load_config(config_path)

    docs_dir = config["output"]["dir"]
    state_file = get_state_file_path(docs_dir)

    state = load_state(state_file)

    existing_ids = get_existing_papers(docs_dir)
    print(f"已存在 {len(existing_ids)} 篇论文")

    if state is None:
        start_date = datetime.strptime(
            config["start_date"],
            "%Y-%m-%d",
        ).replace(tzinfo=timezone.utc)

        print(f"首次运行，从 {start_date.strftime('%Y-%m-%d')} 开始爬取")

    else:
        last_paper_date_str = state["last_run_date"]

        start_date = datetime.strptime(
            last_paper_date_str,
            "%Y-%m-%d",
        ).replace(tzinfo=timezone.utc)

        print(f"从最新论文日期 {start_date.strftime('%Y-%m-%d UTC')} 开始爬取")

    end_date = datetime.now(timezone.utc)

    if start_date > end_date:
        print("日期范围无效，退出")
        return

    client = arxiv.Client(
        page_size=100,
        delay_seconds=ARXIV_API_DELAY,
        num_retries=ARXIV_MAX_RETRIES,
    )

    papers_by_category = {}
    max_results = config["output"]["max_papers_per_category"]
    keywords = config.get("keywords", [])
    latest_paper_date = None

    for i, category in enumerate(config["categories"]):
        papers, category_latest = fetch_papers_by_category(
            client,
            category,
            start_date,
            end_date,
            max_results,
            keywords,
        )

        papers_by_category[category] = papers

        if category_latest is not None:
            if latest_paper_date is None or category_latest > latest_paper_date:
                latest_paper_date = category_latest

        if i < len(config["categories"]) - 1:
            print(f"  等待 {ARXIV_API_DELAY:.0f} 秒以遵守 API 使用条款...")
            time.sleep(ARXIV_API_DELAY)

    papers_by_date, new_count = generate_markdown(
        papers_by_category,
        config,
        existing_ids,
    )

    if new_count > 0:
        files_created, total_papers = update_markdown_files_by_date(
            papers_by_date,
            config,
        )
        print(f"✓ 成功创建 {files_created} 个文件，包含 {total_papers} 篇新论文到 {docs_dir}")
    else:
        print("✓ 没有新论文需要添加")

    if latest_paper_date is not None:
        state_date = latest_paper_date
        print(f"保存最新论文日期: {state_date.strftime('%Y-%m-%d')}")
        save_state(state_file, state_date)
    else:
        if state:
            print(f"没有新论文，保持状态不变: {state['last_run_date']}")
        else:
            print("没有新论文，状态文件尚未创建")

    save_last_update_time(end_date)

    print(f"\n完成！共爬取 {new_count} 篇新论文")


if __name__ == "__main__":
    main()
