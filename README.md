# Oriole-bench: automated benchmarking for Postgres and OrioleDB

Benchmark is designed to run machines with big number of cores and RAM e.g. AWS c7g/c7gd Runing the test 
Mainly fo internal usage. Use at your own risk.

## Usage

```
git checkout https://github.com/pashkinelfe/oriole-bench.git
cd oriole-bench
ORIOLE_ID="{list of orioledb commits}" PG_ID="{list of pg commits}" ./tests.sh
```

```{list of orioledb commits}``` - a list of oriole commit hashes, tags or branch names to be compared in tests

```{list of pg commits}``` (optional) - list of PG commit hashes, tags or branch names to be compared in tests

It is used at git checkout stage.

Result files would be like: ```./results/<orioledb/heap>-<commit hash/tag>-<test-name>-<optional params>```

Each result file contains timestamp then test results. Repeated test runs appended to the file with respective timestamps.

## Advanced options
```FAST_RUN=1``` Run fast tests for debug. Not recommended for actual measurements 

```NVME=1``` Create and mount NVME volume and use it as ```pgdata``` destination for tests. 
This is compatible with volumes layout of c7gd instances. Don't run on EBS-only instances.

```TESTS_LIST="{list of tests to run}"``` (optional) list of test suites to run from ```tpcc```, ```pgbench```, ```ibench```. When not specified all will run.

### Pgbench-based tests

```PRECISE_PGBENCH=1``` - Gather more points for smooth and beautiful connections plot. Takes more time

```PGBENCH_TESTS_LIST="{list of pgbench tests to run}"``` (optional) list of pgbench tests to run from ```select```, ```select_any```, ```tpcb```, ```tpcb_procedure```. When not specified all tests will run.

### Tpc-c test

```$WAREHOUSES``` (optional) - List of numbers of warehouses to run test on 

```$LINEAR_SCALE=1``` - Connections are on linear scale. Beautiful for publishing but slow 

```$INIT_POINT=1``` - Init database before each point not before each series. Slower but more repeatable at large scales due to the same OS file buffers state before each measurement

## Example

``` ORIOLE_ID="main beta9 f55152254" PG_ID="master" ./tests.sh ```

This will perform all tests with default options on the current ```master``` branch of PG and on three states of OrioleDB: branch ```main```, tag ```beta9```, commit ```f55152254```.

## Limitations

### Tpc-c test

With default options ```pgdata``` will need around 80Gb

### Ibench test

With default options ```pgdata``` will need around 200Gb. Each test for Orioledb takes 4-5 hours, for PG 15 hours

## Caveats

- Benchmarks parameters are chosen for quite heavy instances like c7g. For running on smaller machines PG config parameters and test scales may need to be modified. 
- Error processing is far from being full. If you get something unexpected - report.
- Ibench test detaches several runners, kill them all if stopping test before it finishes.

## Acknowlegements to

Mark Callagan <https://github.com/mdcallag> for ibench tests repo

