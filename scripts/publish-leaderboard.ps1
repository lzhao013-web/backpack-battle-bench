[CmdletBinding()]
param(
    [string]$Database = ".bbbench/results.sqlite3",
    [string]$Snapshot = "leaderboard/results.json",
    [string]$BuildOutput = ".bbbench/pages",
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$CommitMessage = ":chart_with_upwards_trend: update public leaderboard",
    [switch]$LocalOnly,
    [switch]$SkipBuild,
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-GitmojiCommitMessage {
    param(
        [Parameter(Mandatory)]
        [string]$Message
    )

    $tokens = [regex]::Matches($Message, ":[a-z0-9_+-]+:")
    if (
        $Message -notmatch '^:[a-z0-9_+-]+: [^\r\n]+$' -or
        $tokens.Count -ne 1 -or
        $Message -match '^:[a-z0-9_+-]+: (\(|[^ ]+:)'
    ) {
        throw "提交信息必须使用 '<gitmoji shortcode> <message>' 格式，且只能包含一个 Gitmoji，例如 ':chart_with_upwards_trend: update public leaderboard'。"
    }
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)]
        [string]$Command,
        [Parameter(ValueFromRemainingArguments)]
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $Command $($Arguments -join ' ')"
    }
}

function Ensure-PagesEnabled {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Warning "未找到 gh，无法检查 GitHub Pages 是否已启用。"
        return
    }

    & gh api "repos/{owner}/{repo}/pages" *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "首次发布：启用 GitHub Pages（GitHub Actions）..." -ForegroundColor Cyan
    Invoke-Native gh api --method POST "repos/{owner}/{repo}/pages" -f build_type=workflow | Out-Null
}

function Wait-PagesDeployment {
    param(
        [Parameter(Mandatory)]
        [string]$CommitSha
    )

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Warning "未找到 gh，GitHub Actions 已由 push 触发，但无法等待部署结果。"
        return
    }

    Write-Host "等待 GitHub Pages workflow 出现..." -ForegroundColor Cyan
    $runId = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $json = & gh run list `
            --workflow pages.yml `
            --branch $Branch `
            --commit $CommitSha `
            --limit 1 `
            --json databaseId 2>$null
        if ($LASTEXITCODE -eq 0 -and $json) {
            $runs = @($json | ConvertFrom-Json)
            if ($runs.Count -gt 0 -and $null -ne $runs[0].databaseId) {
                $runId = $runs[0].databaseId
                break
            }
        }
        Start-Sleep -Seconds 2
    }

    if (-not $runId) {
        throw "push 已完成，但没有找到对应的 Pages workflow。请检查 GitHub Actions。"
    }

    Invoke-Native gh run watch ([string]$runId) --exit-status
    $runUrl = & gh run view ([string]$runId) --json url --jq .url
    if ($LASTEXITCODE -eq 0 -and $runUrl) {
        Write-Host "Actions: $runUrl" -ForegroundColor Green
    }

    $pagesUrl = & gh api "repos/{owner}/{repo}/pages" --jq .html_url 2>$null
    if ($LASTEXITCODE -eq 0 -and $pagesUrl) {
        Write-Host "Leaderboard: $pagesUrl" -ForegroundColor Green
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
    Assert-GitmojiCommitMessage -Message $CommitMessage

    if ($LocalOnly -and -not $PSBoundParameters.ContainsKey("Snapshot")) {
        $Snapshot = ".bbbench/leaderboard-results.local.json"
    }

    $requiredCommands = @("uv")
    if (-not $LocalOnly) {
        $requiredCommands += "git"
    }
    foreach ($required in $requiredCommands) {
        if (-not (Get-Command $required -ErrorAction SilentlyContinue)) {
            throw "未找到命令：$required"
        }
    }

    if (-not $LocalOnly) {
        Invoke-Native git rev-parse --is-inside-work-tree | Out-Null
        $currentBranch = (& git branch --show-current).Trim()
        if ($LASTEXITCODE -ne 0 -or $currentBranch -ne $Branch) {
            throw "当前分支是 '$currentBranch'，请切换到 '$Branch' 后再发布。"
        }

        $staged = @(& git diff --cached --name-only)
        if ($LASTEXITCODE -ne 0) {
            throw "无法检查暂存区。"
        }
        if ($staged.Count -gt 0) {
            throw "暂存区已有内容，请先提交或取消暂存，避免混入排行榜提交：$($staged -join ', ')"
        }

        foreach ($requiredInHead in @(
            ".github/workflows/pages.yml",
            "src/backpack_bench/static_site.py"
        )) {
            & git cat-file -e "HEAD:$requiredInHead" 2>$null
            if ($LASTEXITCODE -ne 0) {
                throw "'$requiredInHead' 尚未提交。请先提交并推送排行榜基础功能，再用本脚本更新成绩。"
            }
        }

        Ensure-PagesEnabled
    }

    if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
        throw "找不到结果数据库：$Database"
    }

    Write-Host "[1/4] 导出公开排行榜快照" -ForegroundColor Cyan
    Invoke-Native uv run --frozen bbbench site snapshot `
        --workspace $repoRoot `
        --database $Database `
        --output $Snapshot

    if (-not $SkipBuild) {
        Write-Host "[2/4] 本地构建静态站点" -ForegroundColor Cyan
        Invoke-Native uv run --frozen bbbench site build `
            --workspace $repoRoot `
            --snapshot $Snapshot `
            --output $BuildOutput
    }
    else {
        Write-Host "[2/4] 已跳过本地静态构建" -ForegroundColor DarkGray
    }

    if ($LocalOnly) {
        Write-Host "本地校验完成，未提交或推送任何内容。" -ForegroundColor Green
        Write-Host "快照：$Snapshot"
        if (-not $SkipBuild) {
            Write-Host "站点：$BuildOutput"
        }
        return
    }

    & git ls-files --error-unmatch -- $Snapshot >$null 2>$null
    $snapshotTracked = $LASTEXITCODE -eq 0
    & git diff --quiet -- $Snapshot
    $diffExitCode = $LASTEXITCODE
    if ($diffExitCode -gt 1) {
        throw "无法检查排行榜快照差异：$Snapshot"
    }
    $snapshotChanged = (-not $snapshotTracked) -or $diffExitCode -eq 1
    if (-not $snapshotChanged) {
        Write-Host "排行榜快照没有变化，无需创建提交。" -ForegroundColor Yellow
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            Write-Host "触发一次 Pages 重新部署..." -ForegroundColor Cyan
            Invoke-Native gh workflow run pages.yml --ref $Branch
            if (-not $NoWait) {
                Write-Warning "workflow_dispatch 没有新 commit SHA，已触发但不自动等待；可用 gh run watch 查看。"
            }
        }
        else {
            Write-Warning "未安装 gh，无法在无改动时强制重新部署；当前线上内容已是最新快照。"
        }
        return
    }

    Write-Host "[3/4] 提交排行榜快照" -ForegroundColor Cyan
    Invoke-Native git add -- $Snapshot
    Invoke-Native git commit -m $CommitMessage
    $commitSha = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取提交 SHA。"
    }

    Write-Host "[4/4] 推送并触发 GitHub Pages" -ForegroundColor Cyan
    Invoke-Native git push $Remote "HEAD:$Branch"

    if (-not $NoWait) {
        Wait-PagesDeployment -CommitSha $commitSha
    }
    else {
        Write-Host "已推送；GitHub Pages 将在后台部署。" -ForegroundColor Green
    }
}
finally {
    Pop-Location
}
