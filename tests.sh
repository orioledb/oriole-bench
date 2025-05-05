# Do all benchmarks for specified version of PG and Orioledb
# 
# Input:
# $ORIOLE_ID compulsory list of Orioledb commit hashes
# $PG_ID optional list of PG commit hashes (for PG-only tests)
# $FAST_RUN - run fast for testing, not for actual measurements

# ---- BUILD PHASE ----
if [ -z "$ORIOLE_ID" ]; then
	echo "Specify at least one orioledb state in ORIOLE_ID"
	exit 1
fi
set -x
rm -Rf ./orioledb
rm -Rf ./postgres-oriole
rm -Rf ./postgres-master
rm -Rf ./pgbin
git clone https://github.com/orioledb/orioledb
git clone https://github.com/orioledb/postgres postgres-oriole
git clone https://github.com/postgres/postgres.git postgres-master
chmod +x ./orioledb/ci/prerequisites.sh
export COMPILER=clang
export LLVM_VER=17
export CC=$COMPILER-$LLVM_VER
export CHECK_TYPE=normal
export GITHUB_ENV=tmp
#export GITHUB_JOB=custom
./orioledb/ci/prerequisites.sh


for var in $ORIOLE_ID
do
        export GITHUB_WORKSPACE="$(pwd)/pgbin/$var"

	cd orioledb
        git checkout $var
	PATCHSET=`cat .pgtags | grep 17 | cut -d' ' -f2-`
        echo "checkout patchset: $PATCHSET"
	cd ../postgres-oriole
	git checkout $PATCHSET

       	OLDPATH=$PATH
	export PATH=$GITHUB_WORKSPACE/bin:$PATH
	./configure --disable-debug --disable-cassert --enable-tap-tests --with-icu --prefix=$GITHUB_WORKSPACE CFLAGS="-O3"
	make -j `nproc` -s
	make -j `nproc` -s install
	make -C contrib -j `nproc` -s
	make -C contrib -j `nproc` -s install
        cd ../orioledb
	make -j `nproc` USE_PGXS=1 IS_DEV=1
	make -j `nproc` USE_PGXS=1 IS_DEV=1 install
	make -j `nproc` USE_PGXS=1 IS_DEV=1 clean
	cd ..
	export PATH=$OLDPATH
done

if [ -n "$PG_ID" ]; then
	for var in $PG_ID
	do
		export GITHUB_WORKSPACE="$(pwd)/pgbin/$var"
		OLDPATH=$PATH
		export PATH=$GITHUB_WORKSPACE/bin:$PATH
		cd postgres-master
		echo "checkout: $PG_ID"
		git checkout $PG_ID
		./configure --disable-debug --disable-cassert --enable-tap-tests --with-icu --prefix=$GITHUB_WORKSPACE CFLAGS="-O3"
		make -j `nproc` -s
		make -j `nproc` -s  install
		make -C contrib -j `nproc` -s
		make -C contrib -j `nproc` -s install
		cd ..
		export PATH=$OLDPATH
	done
fi

# ---- PREPARE TESTS PHASE
rm -Rf ./mdcallag-tools
rm -Rf ./go-tpc

pip3 install psycopg2 six testgres
git clone https://github.com/pashkinelfe/mdcallag-tools.git mdcallag-tools
export IBENCHDIR=/mdcallag-tools/bench/ibench

GO_VERSION="1.21.1"
wget https://dl.google.com/go/go${GO_VERSION}.linux-arm64.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf go${GO_VERSION}.linux-arm64.tar.gz
if ! grep -q "/usr/local/go/bin" ~/.profile; then
    echo "Setting up Go PATH..."
    echo "export PATH=\$PATH:/usr/local/go/bin" >> ~/.profile
    echo "export GOPATH=\$HOME/go" >> ~/.profile
    source ~/.profile
else
    echo "Go PATH already set."
fi
rm go${GO_VERSION}.linux-arm64.tar.gz
go version
git clone https://github.com/pingcap/go-tpc.git
cd go-tpc
GO15VENDOREXPERIMENT="1" CGO_ENABLED=0 GOARCH=arm64 GO111MODULE=on go build -ldflags '-X "main.version=v1.0.10" -X "main.commit=01c06538227a49fa8f0953cfdf3146a95b4a34a3" -X "main.date=2024-10-29 03:01:30"' -o ./bin/go-tpc cmd/go-tpc/*
cd ..

sudo mkdir /ssd

if [ -n "$NVME" ]; then
	echo "Mounting NVME volume"
#       hardcoded for c7gd instance
	NVME_VOL=`sudo parted -l -m | grep -m 1 "Amazon EC2 NVMe Instance Storage" | cut -f1 -d ':'`
	if [-z $NVME_VOL]; then
		echo "NVME volume $NVME_VOL not found. Try calling without NVME variable."
		exit 1
	fi

	sudo parted $NVME_VOL mklabel gpt
	sudo parted $NVME_VOL mkpart ext4 0% 100%
	sudo mkfs.ext4 "$NVME_VOL"p1
	sudo mount -t ext4 -o defaults,nocheck "$NVME_VOL"p1 /ssd
	sudo chmod 0777 /ssd
	echo "Sucessfully mounted NVME volume"
fi

sudo chmod 0777 /ssd
export PGDATADIR=/ssd/pgdata
sudo mkdir ./results
sudo chmod 0777 ./results

# ---- TEST PHASE ----

if [ -n "$FAST_RUN" ]; then
	echo "FAST RUN $FAST_RUN"
fi

if [ -z "$TESTS_LIST" ]; then
	export TESTS_LIST="tpcc pgbench ibench"
fi

for var in $ORIOLE_ID
do
	export GITHUB_WORKSPACE="$(pwd)/pgbin/$var"
	OLDPATH=$PATH
	export PATH=$GITHUB_WORKSPACE/bin:$PATH
	echo $PATH

	for var in $TESTS_LIST
	do
		ENGINE=orioledb PATCH_ID=$var ./test-$var.sh
	done

	export PATH=$OLDPATH
done

if [ -n "$PG_ID" ]; then
	for var in $PG_ID
	do
	export GITHUB_WORKSPACE="$(pwd)/pgbin/$var"
	OLDPATH=$PATH
	export PATH=$GITHUB_WORKSPACE/bin:$PATH

	for var in $TESTS_LIST
	do
		ENGINE=heap PATCH_ID=$var ./test-$var.sh
	done

	export PATH=$OLDPATH
	done
fi

echo "Oriole-bench tests finished"
