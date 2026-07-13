#!/usr/bin/env bash

set -Eeuo pipefail

database=".bbbench/results.sqlite3"
runs_dir="leaderboard/runs"
snapshot=".bbbench/leaderboard-results.local.json"
build_output=".bbbench/pages"
remote="origin"
branch="main"
commit_message=":chart_with_upwards_trend: update public leaderboard"
local_only=false
skip_build=false
no_wait=false
runs_dir_explicit=false

usage() {
    cat <<'EOF'
Usage: scripts/publish-leaderboard.sh [options]

Options:
  --database PATH          Results database (default: .bbbench/results.sqlite3)
  --runs-dir PATH          Per-run public data (default: leaderboard/runs)
  --snapshot PATH          Aggregated local snapshot (default: .bbbench/leaderboard-results.local.json)
  --build-output PATH      Local site output (default: .bbbench/pages)
  --remote NAME            Git remote (default: origin)
  --branch NAME            Publish branch (default: main)
  --commit-message TEXT    Gitmoji commit subject
  --local-only             Validate locally without committing or pushing
  --skip-build             Skip the local static site build
  --no-wait                Do not wait for GitHub Pages deployment
  -h, --help               Show this help
EOF
}

sync_publish_branch() {
    local remote_head=""

    printf '同步远端发布分支...\n'
    git fetch "$remote" "$branch"
    remote_head=$(git rev-parse FETCH_HEAD)
    if git merge-base --is-ancestor "$remote_head" HEAD; then
        return
    fi
    if git merge-base --is-ancestor HEAD "$remote_head"; then
        git merge --ff-only "$remote_head"
        return
    fi
    printf '本地与远端都有新提交，正在 rebase 后合并各机器的 Run...\n'
    git rebase --autostash "$remote_head" ||
        die "无法自动合并远端提交；请解决 rebase 冲突后重新发布。"
}

push_with_rebase_retry() {
    local remote_head=""

    for attempt in 1 2 3; do
        if git push "$remote" "HEAD:$branch"; then
            return
        fi
        ((attempt < 3)) || break
        printf '远端刚收到其他发布，重新合并后重试（%d/3）...\n' "$((attempt + 1))"
        git fetch "$remote" "$branch"
        remote_head=$(git rev-parse FETCH_HEAD)
        git rebase --autostash "$remote_head" ||
            die "无法自动合并另一台机器发布的 Run；请解决 rebase 冲突后重试。"
    done
    die "推送失败；已重试 3 次。"
}

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "未找到命令：$1"
}

ensure_pages_enabled() {
    if ! command -v gh >/dev/null 2>&1; then
        printf '警告：未找到 gh，无法检查 GitHub Pages 是否已启用。\n' >&2
        return
    fi

    if gh api 'repos/{owner}/{repo}/pages' >/dev/null 2>&1; then
        return
    fi

    printf '首次发布：启用 GitHub Pages（GitHub Actions）...\n'
    gh api --method POST 'repos/{owner}/{repo}/pages' -f build_type=workflow >/dev/null
}

assert_gitmoji_commit_message() {
    local message="$1"
    local format_pattern='^:[a-z0-9_+-]+: [^[:cntrl:]]+$'
    local scope_pattern='^:[a-z0-9_+-]+: (\(|[^[:space:]]+:)'
    local token_pattern=':[a-z0-9_+-]+:'
    local remainder="$message"
    local token_count=0

    if [[ ! "$message" =~ $format_pattern ]]; then
        die "提交信息必须使用 '<gitmoji shortcode> <message>' 格式。"
    fi
    while [[ "$remainder" =~ $token_pattern ]]; do
        ((token_count += 1))
        remainder=${remainder#*"${BASH_REMATCH[0]}"}
    done
    if ((token_count != 1)) || [[ "$message" =~ $scope_pattern ]]; then
        die "提交信息只能包含一个 Gitmoji，且不能使用 scope 或冒号分隔，例如 ':chart_with_upwards_trend: update public leaderboard'。"
    fi
}

wait_pages_deployment() {
    local commit_sha="$1"
    local run_id=""
    local run_url=""
    local pages_url=""

    if ! command -v gh >/dev/null 2>&1; then
        printf '警告：未找到 gh，GitHub Actions 已由 push 触发，但无法等待部署结果。\n' >&2
        return
    fi

    printf '等待 GitHub Pages workflow 出现...\n'
    for _ in {1..30}; do
        run_id=$(gh run list \
            --workflow pages.yml \
            --branch "$branch" \
            --commit "$commit_sha" \
            --limit 1 \
            --json databaseId \
            --jq '.[0].databaseId // empty' 2>/dev/null || true)
        if [[ -n "$run_id" ]]; then
            break
        fi
        sleep 2
    done

    [[ -n "$run_id" ]] || die "push 已完成，但没有找到对应的 Pages workflow。请检查 GitHub Actions。"

    gh run watch "$run_id" --exit-status
    run_url=$(gh run view "$run_id" --json url --jq .url 2>/dev/null || true)
    [[ -z "$run_url" ]] || printf 'Actions: %s\n' "$run_url"

    pages_url=$(gh api 'repos/{owner}/{repo}/pages' --jq .html_url 2>/dev/null || true)
    [[ -z "$pages_url" ]] || printf 'Leaderboard: %s\n' "$pages_url"
}

while (($#)); do
    case "$1" in
        --database)
            (($# >= 2)) || die "--database 缺少参数"
            database="$2"
            shift 2
            ;;
        --snapshot)
            (($# >= 2)) || die "--snapshot 缺少参数"
            snapshot="$2"
            shift 2
            ;;
        --runs-dir)
            (($# >= 2)) || die "--runs-dir 缺少参数"
            runs_dir="$2"
            runs_dir_explicit=true
            shift 2
            ;;
        --build-output)
            (($# >= 2)) || die "--build-output 缺少参数"
            build_output="$2"
            shift 2
            ;;
        --remote)
            (($# >= 2)) || die "--remote 缺少参数"
            remote="$2"
            shift 2
            ;;
        --branch)
            (($# >= 2)) || die "--branch 缺少参数"
            branch="$2"
            shift 2
            ;;
        --commit-message)
            (($# >= 2)) || die "--commit-message 缺少参数"
            commit_message="$2"
            shift 2
            ;;
        --local-only)
            local_only=true
            shift
            ;;
        --skip-build)
            skip_build=true
            shift
            ;;
        --no-wait)
            no_wait=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1"
            ;;
    esac
done

assert_gitmoji_commit_message "$commit_message"

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if [[ "$local_only" == true && "$runs_dir_explicit" == false ]]; then
    runs_dir=".bbbench/leaderboard-runs.local"
fi

require_command uv
if [[ "$local_only" == false ]]; then
    require_command git
    git rev-parse --is-inside-work-tree >/dev/null

    current_branch=$(git branch --show-current)
    [[ "$current_branch" == "$branch" ]] ||
        die "当前分支是 '$current_branch'，请切换到 '$branch' 后再发布。"

    staged=$(git diff --cached --name-only)
    [[ -z "$staged" ]] || die "暂存区已有内容，请先提交或取消暂存，避免混入排行榜提交：$staged"

    for required_in_head in \
        ".github/workflows/pages.yml" \
        "src/backpack_bench/cli.py" \
        "src/backpack_bench/static_site.py"; do
        git cat-file -e "HEAD:$required_in_head" 2>/dev/null ||
            die "'$required_in_head' 尚未提交。请先提交并推送排行榜基础功能，再用本脚本更新成绩。"
    done
    git diff --quiet HEAD -- \
        ".gitignore" \
        ".github/workflows/pages.yml" \
        "scripts/publish-leaderboard.sh" \
        "scripts/publish-leaderboard.ps1" \
        "src/backpack_bench/cli.py" \
        "src/backpack_bench/static_site.py" ||
        die "排行榜发布功能有未提交的修改。请先提交并推送这些代码，再发布成绩。"

    sync_publish_branch
    ensure_pages_enabled
fi

[[ -f "$database" ]] || die "找不到结果数据库：$database"

printf '[1/5] 导出独立 Run 快照\n'
uv run --frozen bbbench site export-runs \
    --workspace "$repo_root" \
    --database "$database" \
    --output "$runs_dir"

printf '[2/5] 聚合全部 Run\n'
uv run --frozen bbbench site aggregate \
    --workspace "$repo_root" \
    --runs-dir "$runs_dir" \
    --baseline "leaderboard/results.json" \
    --output "$snapshot"

if [[ "$skip_build" == false ]]; then
    printf '[3/5] 本地构建静态站点\n'
    uv run --frozen bbbench site build \
        --workspace "$repo_root" \
        --snapshot "$snapshot" \
        --output "$build_output"
else
    printf '[3/5] 已跳过本地静态构建\n'
fi

if [[ "$local_only" == true ]]; then
    printf '本地校验完成，未提交或推送任何内容。\n'
    printf 'Run 快照：%s\n' "$runs_dir"
    printf '快照：%s\n' "$snapshot"
    [[ "$skip_build" == true ]] || printf '站点：%s\n' "$build_output"
    exit 0
fi

run_changes=$(git status --porcelain -- "$runs_dir")
if [[ -z "$run_changes" ]]; then
    printf '没有新的 Run 快照，无需创建提交。\n'
    if command -v gh >/dev/null 2>&1; then
        printf '触发一次 Pages 重新部署...\n'
        gh workflow run pages.yml --ref "$branch"
        if [[ "$no_wait" == false ]]; then
            printf '警告：workflow_dispatch 没有新 commit SHA，已触发但不自动等待；可用 gh run watch 查看。\n' >&2
        fi
    else
        printf '警告：未安装 gh，无法在无改动时强制重新部署；当前线上内容已是最新快照。\n' >&2
    fi
    exit 0
fi

printf '[4/5] 提交独立 Run 快照\n'
git add -- "$runs_dir"
git commit -m "$commit_message"

printf '[5/5] 推送并触发 GitHub Pages\n'
push_with_rebase_retry
commit_sha=$(git rev-parse HEAD)

if [[ "$no_wait" == false ]]; then
    wait_pages_deployment "$commit_sha"
else
    printf '已推送；GitHub Pages 将在后台部署。\n'
fi
