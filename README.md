# Oriole-bench: automated benchmarking for Postgres and OrioleDB

Benchmark is designed to run machines with big number of cores and RAM e.g. AWS c7g/c7gd Runing the test
Mainly for internal usage. Use at your own risk.

## Simple usage

```
git checkout https://github.com/pashkinelfe/oriole-bench.git
cd oriole-bench
ORIOLE_ID="{list of orioledb commits}" PG_ID="{list of pg commits}" ./tests.sh
```

```{list of orioledb commits}``` - a list of oriole commit hashes, tags or branch names to be compared in tests

```{list of pg commits}``` (optional) - list of PG commit hashes, tags or branch names to be compared in tests

Result files would be like: ```./results/<orioledb/heap>-<commit hash/tag>-<test-name>-<optional params>```

Each result file contains timestamp then test results. Repeated test runs appended to the file with their actual timestamps.

## Advanced options
```FAST_RUN=1``` Run fast tests for debug. Not recommended for actual measurements

```NVME=1``` Create and mount NVME volume and use it as ```pgdata``` destination for tests.
This is compatible with volumes layout of c7gd instances. Don't run on EBS-only instances.

```TESTS_LIST="{list of tests to run}"``` (optional) list of test suites to run from ```tpcc```, ```pgbench```, ```ibench```. When not specified all will run.

```MEMORY_BUFFERS="{buffers value}"``` Set customized value of ```shared_buffers```/```orioledb.main_buffers``` for heap/orioledb tests. 

### Pgbench-based tests

* Recommended options for beautiful, more repeatable but slower results:

```PRECISE_PGBENCH=1``` - Gather more points for smooth and beautiful connections plot. Takes more time

* Advanced options:

```PGBENCH_CONNS``` (optional) - List of connections to run test on. This overrides setting of ```$PRECISE_PGBENCH```.

```PGBENCH_TESTS_LIST="{list of pgbench tests to run}"``` (optional) list of pgbench tests to run from ```select```, ```select_any```, ```tpcb```, ```tpcb_procedure```. When not specified all tests will run.

### Tpc-c test

* Recommended options for beautiful, more repeatable but slower results:

```LINEAR_SCALE=1``` - Connections are on linear scale.

```INIT_POINT=1``` - Init database before each point not before each series. More repeatable at large scales due to the same OS file buffers state before each measurement

* Advanced options:

```$WAREHOUSES``` (optional) - List of numbers of warehouses to run test on

```TPCC_CONNS``` (optional) - List of connections to run test on. This overrides setting of ```$LINEAR_SCALE```.

### Ibench tests

```IBENCH_SCALE_MUL``` - Custom scale value. Default is 100. Overrides ```FAST_RUN``` option.

## Examples

Run all tests with default config:
```
ORIOLE_ID="main beta9 f55152254" PG_ID="master" ./tests.sh
```

Run only ```tpcc``` and ```pgbench``` tests with ```shared_buffers```/```orioledb.main_buffers``` to 100GB for heap/orioledb tests. Use settings for beautiful repeatable and slower results in ```tpcc``` test. Within ```pgbench``` test run only ```select_any``` and ```tpcb_procedure``` tests on 10, 50 and 100 connections only:

```
ORIOLE_ID="main beta9 f55152254" PG_ID="master" MEMORY_BUFFERS='100GB' INIT_POINT=1 LINEAR_SCALE=1 TESTS_LISI="pgbench tpcc" PGBENCH_TESTS_LIST="select_any tpcb_procedure" PGBENCH_CONNS="10 50 100" ./tests.sh
```

This will perform all tests with default options on the current ```master``` branch of PG and on three states of OrioleDB: branch ```main```, tag ```beta9```, commit ```f55152254```.

## Limitations

### Tpc-c test

With default options ```pgdata``` will need around 80Gb

### Ibench test

With default options ```pgdata``` will need around 200Gb. Each test for Orioledb takes 4-5 hours, for PG 15 hours

Setting ```MEMORY_BUFFERS``` similar to ```tpcc``` and ```pgbench``` tests might be low for this test. It's recommended
to leave default values.

## Caveats

- Benchmarks parameters are chosen for quite heavy instances like c7g. For running on smaller machines PG config parameters and test scales may need to be modified. 
- Error processing is far from being full. If you get something unexpected - report.
- Ibench test detaches several runners, kill them all if stopping test before it finishes.

## Acknowlegements to

Mark Callagan <https://github.com/mdcallag> for ibench tests repo

