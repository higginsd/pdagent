#
# See howto.txt for instructions.
#

set -e  # fail on errors

# params
case "$1" in
  deb)
        ;;
  rpm)
        ;;
  *)
        echo "Usage: $0 {deb|rpm}"
        exit 2
esac

echo = BUILD TYPE: $1

echo = cleaning build directory
rm -fr data
mkdir data

echo = /usr/bin/...
mkdir -p data/usr/bin
cp ../bin/*.py data/usr/bin

echo = /var/...
mkdir -p data/var/log/pdagent
mkdir -p data/var/lib/pdagent/outqueue

echo = /etc/...
mkdir -p data/etc/pd-agent/
cp ../conf/config.cfg data/etc/pd-agent/
mkdir -p data/etc/init.d
cp ../bin/agent.py data/etc/init.d/pd-agent

if [[ "$1" == "deb" ]]; then
    _PY_SITE_PACKAGES=data/usr/share/pyshared
else
    _PY_SITE_PACKAGES=data/usr/lib/python2.6/site-packages
fi

echo = python modules...
mkdir -p $_PY_SITE_PACKAGES
(cd .. && find pdagent -type d -exec mkdir build-linux/$_PY_SITE_PACKAGES/{} \;)
(cd .. && find pdagent -type f -name "*.py" -exec cp {} build-linux/$_PY_SITE_PACKAGES/{} \;)

if [[ "$1" == "deb" ]]; then
    echo = deb python-support...
    mkdir -p data/usr/share/python-support
    _PD_PUBLIC=data/usr/share/python-support/python-pdagent.public
    echo pyversions=2.6- > $_PD_PUBLIC
    echo >> $_PD_PUBLIC
    find $_PY_SITE_PACKAGES -type f -name "*.py" | cut -c 5- >> $_PD_PUBLIC
    #echo ---- python-pdagent.public:
    #cat $_PD_PUBLIC
    #echo ----
fi

echo = FPM!
_FPM_DEPENDS="--depends python"
if [[ "$1" == "deb" ]]; then
    _FPM_DEPENDS="$_FPM_DEPENDS --depends python-support"
    _PRE_UNINST="--pre-uninstall $1/prerm"
else
    _PRE_UNINST=""
fi
fpm -s dir \
    -t $1 \
    --name "pdagent" \
    --version "0.1" \
    --architecture all \
    $_FPM_DEPENDS \
    --post-install $1/postinst \
    $_PRE_UNINST \
    -C data \
    etc usr var

#    --prefix /usr \
#    --deb-user pdagent
# --config-files /etc/redis/redis.conf -v 2.6.10 ./src/redis-server=/usr/bin redis.conf=/etc/redis

exit 0
