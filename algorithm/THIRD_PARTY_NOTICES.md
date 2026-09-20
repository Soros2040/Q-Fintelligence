# Algorithm dependencies / 算法依赖

Declarations are in [pyproject.toml](pyproject.toml); distribution versions and hashes are in [requirements.lock](requirements.lock) and the optional [notebook lock](requirements-notebooks.lock). Preserve every installed distribution's license when redistributing it. The numerical stack includes NumPy, pandas, SciPy and scikit-learn; quantum functionality uses cqlib. Installed metadata is the source of truth for the license of a specific build, including bundled native libraries.

Dependencies retain their own licenses. SDK use and external platform access also follow provider terms. See the root [notice](../THIRD_PARTY_NOTICES.md) for other components.

依赖声明见项目配置，精确版本和哈希见锁文件。分发依赖时保留每个发行包的许可，包括打包的原生库声明。各依赖保留原许可，项目 MIT 许可不为其重新授权。SDK 与平台访问还遵循提供方服务条款。
