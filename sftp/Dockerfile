# atmoz/sftp con llaves de host persistentes (para que no cambien al recrear el contenedor).
ARG IMAGEN_SFTP=atmoz/sftp:alpine
FROM ${IMAGEN_SFTP}
COPY llaves_persistentes.sh /etc/sftp.d/llaves_persistentes.sh
RUN chmod 755 /etc/sftp.d/llaves_persistentes.sh && sed -i 's/\r$//' /etc/sftp.d/llaves_persistentes.sh
