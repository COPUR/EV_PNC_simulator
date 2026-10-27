# Fetch the pinned hub contracts, run the tests, build the container.
CONTRACTS_REPO := $(shell sed -n 's/^repo=//p' contracts.lock)
CONTRACTS_COMMIT := $(shell sed -n 's/^commit=//p' contracts.lock)

.PHONY: contracts test lint image smoke

contracts:
	rm -rf .contracts && git init -q .contracts
	git -C .contracts fetch -q --depth 1 $(CONTRACTS_REPO) $(CONTRACTS_COMMIT)
	git -C .contracts checkout -q FETCH_HEAD

test:
	uv run --extra test pytest -q

lint:
	uvx ruff@0.16.9 check src tests
	uvx ruff@0.16.9 format --check src tests

image: contracts
	docker build -t ev-pnc-simulator:dev .

# Start the container and check the admin API and one served operation answer.
smoke:
	docker run -d --rm --name evpnc-sim-smoke -p 18080:8080 ev-pnc-simulator:dev
	for i in $$(seq 1 30); do curl -fs localhost:18080/_mock/health >/dev/null && break; sleep 1; done
	curl -fs localhost:18080/_mock/health | grep -q '"mock":"hubject"' || (docker logs evpnc-sim-smoke; docker stop evpnc-sim-smoke; exit 1)
	curl -fs -XPOST localhost:18080/evsepull/v21/providers/TRTST/status-records -H 'content-type: application/json' -d '{"ProviderID":"TRTST"}' | grep -q EvseStatuses
	docker stop evpnc-sim-smoke
