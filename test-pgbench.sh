# Run pgbench tests
# Input parameters
# $PATCH_ID - commit hash
# $ENGINE - heap or orioledb
# $PGDATADIR - PG data dir
# $PRECISE_PGBENCH - measure more connection points than usual
# $FAST_RUN - run fast for testing, not for actual measurements

RESULTFILE="results/$ENGINE-$PATCH_ID-pgbench"

echo TESTING PATCH $PATCH_ID

# Check correct path to PG build
if [ `which pg_ctl` = "/usr/local/pgsql/bin/pg_ctl" ]; then
	echo "USING DEFAULT PG BINARIES. CHECK THAT bin DIRECTORY OF YOUR PATCHSET IS SET ON A FIRST POSITION IN PATH"
	exit 1
fi

pg_ctl -D $PGDATADIR -l logfile stop
rm -Rf /ssd/pgdata
initdb $PGDATADIR --no-locale
pg_ctl -D $PGDATADIR -l logfile start

cp postgresql.auto.conf.pgbench $PGDATADIR/postgresql.auto.conf
if [ $ENGINE = "orioledb" ]; then
        psql -dpostgres -c "create extension orioledb;"
        cat postgresql.auto.conf.orioledb.pgbench >> $PGDATADIR/postgresql.auto.conf
elif [ $ENGINE = "heap" ]; then
        cat postgresql.auto.conf.heap.pgbench >> $PGDATADIR/postgresql.auto.conf
else
        echo "Unknown engne: $ENGINE"
        exit 1
fi

pg_ctl -D $PGDATADIR -l logfile restart

pgbench postgres -i -s100
psql -dpostgres -f ./orioledb-prepare-function.sql

if [ -n "$PRECISE_PGBENCH" ]; then
	conns=(5 6 7 8 9 10 11 12 13 15 16 18 20 22 24 27 30 33 36 39 43 47 51 56 62 68 75 82 91 100 110 120 130 150 160 180 200 220 240 270 300 330 360 390 430 470)
else
	conns=(10 15 22 33 47 68 100 150 220 330 470)
fi

if [ -n "$FAST_RUN" ]; then
	FAST_RUN_MSG="FAST RUN!"
	RUN_TIME=5
else
	RUN_TIME=30
fi


echo "# $FAST_RUN_MSG " `date` >> $RESULTFILE
echo "# conns, tps" >> $RESULTFILE

if [ -z "$PGBENCH_TESTS_LIST" ]
	export PGBENCH_TEST_LIST="select, select_any, tpcb, tpcb_procedure"
fi

for t in $PGBENCH_TESTS_LIST
do
	if [ $t = "select" ]; then
		echo "# Random select test" >> $RESULTFILE
		for a in "${conns[@]}"
		do
			echo "read only test conns: $a"
			echo $a | tr '\n' ',' >> $RESULTFILE
			psql -dpostgres -c "checkpoint;"
			pgbench postgres -S -M prepared -T $RUN_TIME -j 5 -c $a | grep "tps = " | grep "(without initial connection time)" | cut -d " " -f3 | cut -d "." -f1 >> $RESULTFILE
		done

	elif [ $t = "select_any" ]; then
		echo "# Select any random 9 test" >> $RESULTFILE
		for a in "${conns[@]}"
		do
			echo "select 9 conns: $a"
			echo $a | tr '\n' ',' >> $RESULTFILE
			psql -dpostgres -c "checkpoint;"
			pgbench postgres -f ./orioledb-select-9.sql -s100 -M prepared -T $RUN_TIME -j 5 -c $a | grep "tps = " | grep "(without initial connection time)" | cut -d " " -f3 | cut -d "." -f1 >> $RESULTFILE
		done

	elif [ $t = "tpcb_procedure" ]; then
		echo "# tpc-b in procedure test" >> $RESULTFILE
		for a in "${conns[@]}"
		do
			echo "tpcb procedure conns: $a"
			echo $a | tr '\n' ',' >> $RESULTFILE
			psql -dpostgres -c "checkpoint;"
			pgbench postgres -f ./orioledb-tpcb-in-procedure.sql -s100 -M prepared -T $RUN_TIME -j 5 -c $a | grep "tps = " | grep "(without initial connection time)" | cut -d " " -f3 | cut -d "." -f1 >> $RESULTFILE
		done

	elif [ $t = "tpcb" ]; then
		echo "# tpc-b test" >> $RESULTFILE
		for a in "${conns[@]}"
		do
			echo "TPC-b conns: $a"
			echo $a | tr '\n' ',' >> $RESULTFILE
			psql -dpostgres -c "checkpoint;"
			pgbench postgres -M prepared -T $RUN_TIME -j 5 -c $a | grep "tps = " | grep "(without initial connection time)" | cut -d " " -f3 | cut -d "." -f1 >> $RESULTFILE
		done

	else
		echo "# unknown pgbench test"
		exit 1

	fi

done
pg_ctl -D $PGDATADIR -l logfile stop
