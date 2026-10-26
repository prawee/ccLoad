---
name: ccload-release
description: 发布 ccLoad release 分支的稳定版本，完成预检、提交、推送、Actions、GitHub Release 和 GHCR 多架构镜像验证；仅检查或修改发布流程时不执行发布。
---

# 发布 ccLoad

通过 push 到 `release` 分支发布，不使用 `.github/workflows/release.yml` 的 tag-trigger 发布流程。`.github/workflows/release-branch-image.yml` 是唯一发布入口：它构建 `linux/amd64` 与 `linux/arm64`，根据最近 stable tag 自动增加 patch，创建 annotated version tag、GitHub Release notes，并将镜像推送至 GHCR。不要手动创建 tag、Release 或单独发布镜像，也不要手动触发 workflow。

只要求计算版本或预检时，执行下方 dry-run 并报告结果，不进入 publish。用户明确要求发布或单独调用本技能时才可自动提交和普通 push；编辑本 skill 不执行发布。

## 参数契约

- `$ccload-release`、`/ccload-release`、`ccload-release stable`：通过 `release` 分支发布下一个 patch stable version。
- `beta`、`preview` 不支持：release-branch workflow 只创建 stable releases；若用户要求 Beta，停止并解释。
- 默认 push 流程由 workflow 按最近 stable tag 自动增加 patch。若用户明确指定 stable version（例如 `v1.0.2`），先验证 tag 不存在，再用 `workflow_dispatch` 的 `release_version` 输入发布该精确版本；不要假设自动 patch 计算会得到指定版本。

Tag 形状为 `vX.Y.Z`，workflow 将同一镜像发布为 `release`、version tag 与 `release-<commit-sha>`。

## 发布前检查与版本确认

1. 检查 `git status --short --branch`、当前分支、`git diff`、`git diff --cached`、`git remote -v`。不要覆盖或提交无关的用户改动。
2. 验证仓库目标 remote 是本次授权发布的 repo，且 `release` 分支已跟踪正确的 `origin/release`。fetch branch 与 tags，检查本地/远端关系；若远端 ahead、diverged、remote 不正确或 tag 状态不完整，停止，不自动 merge/rebase/pull。
3. 查看 stable tags，按 SemVer 排序，计算 workflow 将选择的最高 `vX.Y.Z`；下一个 patch 是 `vX.Y.(Z+1)`。若无 stable tag，则 workflow 起始版本为 `v0.0.1`。
4. 如果用户指定版本（例如 `v1.0.2`），必须确认它与计算的下一个 patch 完全相同，且该 tag 不存在。如果不匹配，停止；不要强行设置版本。
5. 检查触发 workflow 的 commit 和 release notes 范围。workflow 对比上一个 stable tag 到新 tag 的所有 commit subjects；如果没有可用前一 stable tag，就记录 branch 的全部 commit subjects。确认 release branch 包含预期改动。
6. push 到 `release` 会创建 tag 和 GitHub Release，并更新 GHCR 的 `release` alias；发布属于外部可见的不可逆操作。用户明确要求发布后，无需重复询问，但所有前置检查必须通过。

## 发布步骤

1. 工作区必须干净；不得自动 `git add -A` 把无关改动带进 release。若有工作区改动，先审阅并只提交与发布相关的文件，使用准确的 Conventional Commit subject。未经用户要求不要自动创建额外提交。
2. 发布提交必须先进入 `release` 分支。若当前不在该分支，确认本地没有会被覆盖的改动后 checkout `release`；验证与 `origin/release` 同步。不要将 `master` 当作发布分支。
3. 预检 stable tags 和目标 patch version，并确认目标 tag 未存在。以 `git diff --check` 和本次改动相关测试/验证通过作为本地门禁。
4. 使用普通 fast-forward push 到 `origin/release`。正常自动 patch 发布由 push 触发 `.github/workflows/release-branch-image.yml`。若 push 仅用于发布流程配置且要求指定版本，先确保该 push 不会误触发自动版本发布，再使用 GitHub CLI `gh workflow run release-branch-image.yml --ref release -f release_version=vX.Y.Z` dispatch 精确版本；不得手动创建 tag 或 Release。
5. 使用 GitHub CLI 查询对应 Actions run，必须按 workflow 文件名、branch `release` 和完整 commit SHA 匹配；dispatch run 也需核对其输入版本。等待所有相关 jobs 成功。找不到 run、run 失败或取消时，不要伪称发布成功，也不要重试造成重复发布；报告 run URL 与失败状态。
6. 成功后验证远端 tag 和 GitHub Release 的实际版本、release notes、GHCR image tag 与 OCI manifest。image 至少应支持 `linux/amd64` 和 `linux/arm64`，并确认 `release`、version tag、`release-<sha>` 指向相同 digest（若 registry 对 alias 处理使得无法比对，明确报告）。
7. 报告 commit SHA、目标 version、workflow run URL/结论、GitHub Release URL、GHCR image tags 和验证结果。

## 安全规则

- 仅 push `release`；不 push `master`，不创建 tag 手动触发 `.github/workflows/release.yml`，不使用该 skill 的旧 `scripts/release.sh`（该脚本强制 `master` 与旧仓库名，不适用于 release-branch 流程）。
- 不 force push、不自动 rebase/merge、不删除或改写 tag、Release、image。任何失败都保留现场并报告，不自动回滚。
- 此流程只创建 stable version，不适用于 Beta/prerelease。不要把 `beta` 或 `latest` Git tag 假装成发布版本。
- Release branch workflow 当前使用 `GITHUB_TOKEN`。Push 生成的 version tag 不会触发其他 workflow；该 release branch workflow 本身需要成功完成 GitHub Release 与 GHCR 发布。
- Workflow 以 `release` branch 当前 tip 建 tag；若多个 run 并发或 branch 在 workflow 运行期间推进，检查 tag 所指 commit 与本次预期 commit 一致，否则停止并报告。
