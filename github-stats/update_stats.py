#!/usr/bin/env python3
"""Generate GitHub stats and language cards using GitHub APIs.

Only four theme-specific SVG files are generated beside this script.
"""
import html
import json
import os
import sys
import urllib.error
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TOKEN = os.environ.get("GH_STATS_TOKEN")
LOGIN = os.environ.get("GITHUB_USERNAME", "abusofianid")
API = "https://api.github.com/graphql"
THEMES = {
    "dark": {"bg": "#0d1117", "text": "#f0f6fc", "muted": "#c9d1d9", "border": "#8b949e", "panel": "#161b22", "bars": ["#f0f6fc", "#c9d1d9", "#8b949e", "#6e7681", "#484f58", "#30363d"]},
    "light": {"bg": "#ffffff", "text": "#1f2328", "muted": "#424a53", "border": "#6e7781", "panel": "#f6f8fa", "bars": ["#24292f", "#424a53", "#57606a", "#6e7781", "#8c959f", "#afb8c1"]},
}
LANGUAGE_COLORS = {
    "TypeScript":"#3178c6", "JavaScript":"#f1e05a", "CSS":"#663399", "HTML":"#e34c26",
    "Python":"#3572A5", "Rust":"#dea584", "Shell":"#89e051", "Go":"#00ADD8",
    "Java":"#b07219", "C":"#555555", "C++":"#f34b7d", "C#":"#178600",
    "PHP":"#4F5D95", "Ruby":"#701516", "Kotlin":"#A97BFF", "Swift":"#F05138",
    "Dart":"#00B4AB", "Vue":"#41b883", "Svelte":"#ff3e00", "SCSS":"#c6538c",
    "Jupyter Notebook":"#DA5B0B", "Makefile":"#427819", "Dockerfile":"#384d54",
    "CMake":"#DA3434", "Lua":"#000080", "Elixir":"#6e4a7e", "Haskell":"#5e5086",
    "R":"#198CE7", "Zig":"#ec915c", "Nix":"#7e7eff", "Astro":"#ff5a03",
}
REPOS_QUERY = """
query($login:String!,$cursor:String) {
 user(login:$login) {
  repositories(first:100,after:$cursor,ownerAffiliations:OWNER,orderBy:{field:NAME,direction:ASC}) {
   pageInfo { hasNextPage endCursor }
   nodes { nameWithOwner stargazerCount languages(first:20,orderBy:{field:SIZE,direction:DESC}) { edges { size node { name } } } }
  }
 }
}
"""
CONTRIB_QUERY = """
query($login:String!,$from:DateTime!,$to:DateTime!) {
 user(login:$login) {
  contributionsCollection(from:$from,to:$to) {
   totalCommitContributions totalIssueContributions totalPullRequestContributions
   contributionCalendar { totalContributions }
  }
 }
}
"""

CONTRIBUTED_REPOS_QUERY = """
query($login:String!,$cursor:String) {
 user(login:$login) {
  repositoriesContributedTo(first:100,after:$cursor,includeUserRepositories:true,contributionTypes:[COMMIT,ISSUE,PULL_REQUEST,REPOSITORY]) {
   pageInfo { hasNextPage endCursor }
   nodes { nameWithOwner }
  }
 }
}
"""

def graphql(query, variables):
    """Execute a GraphQL query and return its data payload."""
    if not TOKEN:
        raise RuntimeError("GH_STATS_TOKEN secret is missing.")
    req = urllib.request.Request(API, data=json.dumps({"query":query,"variables":variables}).encode(), headers={
        "Authorization":f"Bearer {TOKEN}","Content-Type":"application/json","Accept":"application/vnd.github+json","User-Agent":"profile-stats-generator"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:400]}") from exc
    if body.get("errors"):
        raise RuntimeError("GitHub GraphQL error: " + "; ".join(e.get("message","unknown") for e in body["errors"]))
    return body["data"]

def rest_json(url):
    """Request a GitHub REST endpoint and decode its JSON response."""
    """Read a GitHub REST endpoint with the same token used for GraphQL."""
    if not TOKEN:
        raise RuntimeError("GH_STATS_TOKEN secret is missing.")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-stats-generator",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub REST API HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:400]}") from exc


def count_authored_items(search_type, label):
    """Count unique issues or pull requests authored by the account across accessible repositories."""
    unique_ids = set()
    page = 1
    expected_total = None
    while True:
        query = urllib.parse.urlencode({
            "q": f"author:{LOGIN} is:{search_type}",
            "per_page": 100,
            "page": page,
        })
        result = rest_json(f"https://api.github.com/search/issues?{query}")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError(f"Unexpected GitHub search response while counting {label}.")
        if result.get("incomplete_results"):
            raise RuntimeError(f"GitHub returned incomplete search results while counting {label}; refusing to publish a partial total.")
        if expected_total is None:
            expected_total = result.get("total_count")
            if not isinstance(expected_total, int):
                raise RuntimeError(f"GitHub did not provide a valid total while counting {label}.")
            # GitHub Search API returns at most 1,000 results; never publish a silently truncated count.
            if expected_total > 1000:
                raise RuntimeError(f"{label} count ({expected_total}) exceeds GitHub Search API's 1,000-result limit; use a date-partitioned query before publishing.")
        items = result["items"]
        for item in items:
            item_id = item.get("id")
            if item_id is not None:
                unique_ids.add(item_id)
        if not items or len(unique_ids) >= expected_total or len(items) < 100:
            break
        page += 1
    if len(unique_ids) != expected_total:
        raise RuntimeError(f"Incomplete {label} count: GitHub reported {expected_total}, but only {len(unique_ids)} unique items were retrieved.")
    return len(unique_ids)


def collect():
    """Collect all-time statistics and language usage from GitHub APIs."""
    repos, cursor = [], None
    while True:
        conn = graphql(REPOS_QUERY, {"login":LOGIN,"cursor":cursor})["user"]["repositories"]
        repos.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
        if not cursor:
            raise RuntimeError("Missing repository pagination cursor.")
    langs, stars = defaultdict(int), 0
    for repo in repos:
        stars += repo["stargazerCount"]
        for edge in repo["languages"]["edges"]:
            langs[edge["node"]["name"]] += edge["size"]
    totals = {"contributions":0,"commits":0,"issues":0,"pulls":0}
    year_now = datetime.now(timezone.utc).year
    # Query every calendar year separately, rather than a rolling 365-day window.
    for year in range(2008, year_now + 1):
        c = graphql(CONTRIB_QUERY, {"login":LOGIN,"from":f"{year}-01-01T00:00:00Z","to":f"{year}-12-31T23:59:59Z"})["user"]["contributionsCollection"]
        totals["contributions"] += c["contributionCalendar"]["totalContributions"]
        totals["commits"] += c["totalCommitContributions"]
        # Total Commits uses GitHub's profile contribution data for each calendar year.
        # This keeps the metric aligned with eligible commit contributions, not every branch commit.
        # Issue and PR totals below remain unique authored items across all statuses.
    contributed_repos, contributed_cursor = set(), None
    while True:
        response = graphql(CONTRIBUTED_REPOS_QUERY, {"login":LOGIN,"cursor":contributed_cursor})
        user_data = response.get("user") or {}
        conn = user_data.get("repositoriesContributedTo")
        if conn is None:
            raise RuntimeError("GitHub returned no contributed-repository connection; check token access and GraphQL permissions.")
        contributed_repos.update(node["nameWithOwner"] for node in (conn.get("nodes") or []) if node and node.get("nameWithOwner"))
        page_info = conn.get("pageInfo") or {"hasNextPage":False,"endCursor":None}
        if not page_info["hasNextPage"]:
            break
        contributed_cursor = page_info["endCursor"]
        if not contributed_cursor:
            raise RuntimeError("Missing contributed-repository pagination cursor.")

    totals["issues"] = count_authored_items("issue", "Total Issues Opened")
    totals["pulls"] = count_authored_items("pr", "Total Pull Requests")
    total_bytes = sum(langs.values())
    top = sorted(langs.items(), key=lambda item:item[1], reverse=True)[:6]
    return {"stars":stars,"repo_count":len(repos),"contributed_to":len(contributed_repos),"years":f"2008–{year_now}",**totals,
        "languages":[(name,size*100/total_bytes if total_bytes else 0) for name,size in top]}

def tx(x,y,value,theme,size=12,weight=500,anchor="start"):
    """Render an escaped SVG text element with consistent typography."""
    return f'<text x="{x}" y="{y}" font-family="ui-sans-serif,Segoe UI,Arial,sans-serif" font-size="{size}" font-weight="{weight}" fill="{theme}" text-anchor="{anchor}">{html.escape(str(value))}</text>'

def stats_svg(d, mode):
    """Render the main statistics card for the selected theme."""
    p=THEMES[mode]
    metrics=[
        ("Total Contributions",d["contributions"]),
        ("Total Commits",d["commits"]),
        ("Total Issues Opened",d["issues"]),
        ("Total Pull Requests",d["pulls"]),
        ("Total Stars Earned",d["stars"]),
        ("Repositories Contributed To",d["contributed_to"]),
    ]
    s=[f'<svg xmlns="http://www.w3.org/2000/svg" width="460" height="246" viewBox="0 0 460 246" role="img" aria-labelledby="title desc">',
       f'<title id="title">Abu Sofian\'s GitHub Stats</title>',
       f'<desc id="desc">Live all-time GitHub statistics for {html.escape(LOGIN)}, including contributions to accessible private repositories.</desc>',
       f'<rect x="1" y="1" width="458" height="244" rx="12" fill="{p["bg"]}" stroke="{p["border"]}"/>',
       tx(24,32,"Abu Sofian's GitHub Stats",p["text"],18,700),
       f'<line x1="24" y1="45" x2="436" y2="45" stroke="{p["border"]}" stroke-opacity=".65"/>']
    for i,(label,val) in enumerate(metrics):
        y=72+i*29
        s.extend([tx(26,y,label,p["muted"],12),tx(434,y,f"{val:,}",p["text"],14,700,"end")])
    s.append("</svg>")
    return "\n".join(s)+"\n"

def langs_svg(d,mode):
    """Render the language-usage card for the selected theme."""
    p=THEMES[mode]; s=[f'<svg xmlns="http://www.w3.org/2000/svg" width="460" height="246" viewBox="0 0 460 246" role="img" aria-labelledby="title desc">',
        f'<title id="title">Most Used Languages · {html.escape(LOGIN)}</title>',
        '<desc id="desc">Language bytes aggregated across all owner repositories accessible to the token, including private repositories.</desc>',
        f'<rect x="1" y="1" width="458" height="244" rx="12" fill="{p["bg"]}" stroke="{p["border"]}"/>',
        tx(24,32,"Most Used Languages",p["text"],18,700),
        f'<line x1="24" y1="45" x2="436" y2="45" stroke="{p["border"]}" stroke-opacity=".65"/>']
    langs=d["languages"]
    if not langs:
        s.append(tx(24,80,"No language data available",p["muted"]))
    else:
        # Align the visible top of the chart with the visible top of stats text.
        x,bar_y,bar_w=26,70,408
        s.append(f'<rect x="{x}" y="{bar_y}" width="{bar_w}" height="10" rx="5" fill="{p["panel"]}"/>')
        cursor=x
        for i,(name,pct) in enumerate(langs):
            color=LANGUAGE_COLORS.get(name, p["bars"][i%len(p["bars"])])
            seg=bar_w*pct/100
            if seg>0:
                s.append(f'<rect x="{cursor:.2f}" y="{bar_y}" width="{seg:.2f}" height="10" fill="{color}"/>'); cursor+=seg
        for i,(name,pct) in enumerate(langs):
            # Move language labels slightly lower and increase row spacing to reduce the empty space below the final row.
            col=0 if i<3 else 1; row=i if i<3 else i-3; x0=26+col*204; y=122+row*40
            color=LANGUAGE_COLORS.get(name, p["bars"][i%len(p["bars"])])
            s += [f'<circle cx="{x0+5}" cy="{y-4}" r="4" fill="{color}"/>',tx(x0+16,y,name,p["text"],12,600),tx(x0+(192 if col==0 else 204),y,f"{pct:.1f}%",p["muted"],11,500,"end")]
    s.append("</svg>")
    return "\n".join(s)+"\n"

def main():
    """Generate the four supported theme-specific SVG assets in this folder."""
    d=collect()
    # Do not write any output until all API queries have succeeded.
    # Store all generated assets beside this script in the dedicated folder.
    # This also keeps workflow staging paths stable across scheduled and manual runs.
    output_dir = Path(__file__).resolve().parent
    files = {
        "github-stats-dark.svg": stats_svg(d, "dark"),
        "github-stats-light.svg": stats_svg(d, "light"),
        "github-top-langs-dark.svg": langs_svg(d, "dark"),
        "github-top-langs-light.svg": langs_svg(d, "light"),
    }
    for name, content in files.items():
        # Write only supported theme-specific SVG assets; never recreate legacy aliases.
        (output_dir / name).write_text(content, encoding="utf-8")
    print(f'Generated {len(files)} SVG cards in {output_dir}; scanned {d["repo_count"]} owner repositories; history {d["years"]}.')

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"ERROR: {exc}",file=sys.stderr); sys.exit(1)
