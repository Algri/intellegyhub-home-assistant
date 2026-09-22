FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20 AS pigpio-build

RUN apk add --no-cache build-base git linux-headers
RUN git clone --depth 1 --branch v79 https://github.com/joan2937/pigpio.git /tmp/pigpio \
  && cd /tmp/pigpio \
  && find . -type f -exec sed -i -E 's#/dev/pig(pio|out|err)#/tmp/pig\1#g' {} \; \
  && make \
  && install -m 0755 -d /usr/local/bin /usr/local/lib /usr/local/include \
  && install -m 0755 pigpiod pigs /usr/local/bin/ \
  && install -m 0755 libpigpio.so.1 libpigpiod_if.so.1 libpigpiod_if2.so.1 /usr/local/lib/ \
  && ln -sf libpigpio.so.1 /usr/local/lib/libpigpio.so \
  && ln -sf libpigpiod_if.so.1 /usr/local/lib/libpigpiod_if.so \
  && ln -sf libpigpiod_if2.so.1 /usr/local/lib/libpigpiod_if2.so \
  && install -m 0644 pigpio.h pigpiod_if.h pigpiod_if2.h pigs.h /usr/local/include/

FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20

LABEL \
  io.hass.version="0.5.120" \
  io.hass.type="app" \
  io.hass.arch="aarch64"

WORKDIR /app

RUN apk add --no-cache i2c-tools libgpiod
COPY --from=pigpio-build /usr/local/bin/pig* /usr/local/bin/
COPY --from=pigpio-build /usr/local/lib/libpigpio* /usr/local/lib/
COPY --from=pigpio-build /usr/local/include/pigpio* /usr/local/include/
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app
COPY icon.png /app/icon.png
COPY logo.png /app/logo.png
COPY bundled_custom_components /opt/intellegyhub/bundled_custom_components
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
