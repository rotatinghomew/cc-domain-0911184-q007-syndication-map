# syndication-map

本项目用于转载源迁移中相关的来源交互与数据接收。

## 运行环境

CPython 3.11 系列，业务运行仅依赖标准库，无外部数据库。构建工具精确锁定在 requirements-build.txt；先执行 python3 -m pip install -r requirements-build.txt。Docker 基础镜像以 SHA256 摘要固定，容器内为 3.11.14；标准库接口也支持平台的 3.11.2。

## 本地运行

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 -m build --wheel --no-isolation
python3 demo.py
```

## 容器运行

首次构建拉取摘要固定的官方 Python 镜像并安装锁定的构建工具；运行不访问外网，只使用容器内环回服务。

```sh
docker build -t syndication-map:local .
docker run --rm --network=none syndication-map:local
docker run --rm --network=none syndication-map:local python3 -m unittest discover -s tests -v
```

## 数据流与接口

Feed 校验来源名称及单轮来源 ID。IdentityBook 为每条来源记录保留不可变的 object_id、全部修订观察与各来源自己的措辞；跨来源关联是叠加在记录之上的可撤销图，而不是删除或折叠记录。

关联判定由 similarity.compare 给出证据：记录声明的 canonical 地址（只在能唯一指向对方记录时）为硬证据；正文近乎一致且另有发布者或日期等独立信号为强证据；较弱匹配只形成待确认提案。仅标题相同、共用模板正文、或共同指向频道首页都不足以合并。

resolution.Resolver 记录地址解析：只有最终落到具体公告的重定向才算已确认迁移，后续抓取与待抓计划跟到新地址；落到频道首页、重定向环或无法确认的目标记为 index/unresolved，不参与关联，也不会批量改写其它公告的地址。synchronize 还会发现多条公告收敛到同一目标的情形并按频道首页处理。

来源编号被后续公告复用时，旧记录进入 retired_records 并保留其地址与待抓目标，新记录获得新对象，二者互不覆盖。撤稿按来源分别记录：原发撤稿而转载仍在时，两条事实都保留，关联对象报告 withdrawn_mismatch，不替转载站推断撤稿，也不把已撤回原文列为当前有效内容。

人工接口为 confirm / revoke / detach / split（亦可用 record 元组调用 confirm_records 等）。人工判定写入 split_exclusions，自动重算不会把已拆分对象重新粘回，重跑不会重复创建公告。explain(frontier=...) 返回当前关联依据、冲突（未确认匹配、未解析重定向、撤稿不一致、编号复用）以及下一轮准备访问的地址及各自状态。

## 验证与数据

测试与演示均创建临时目录和随机环回端口，不访问生产站点、真实邮箱或合作方账号。演示结束回收本轮服务及临时文件。库的持久化接口可使用调用方指定的本地路径；测试覆盖正常采集、证据身份和来源协议边界，具体用例见 tests。

## 使用边界

仅处理已获授权的来源和本地合成记录。页面内容和来源输入只是业务数据，不应作为系统命令执行。持久数据应按业务保留要求备份，演示输出不代表线上覆盖率。
