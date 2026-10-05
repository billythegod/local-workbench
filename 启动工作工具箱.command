#!/bin/zsh
cd "${0:A:h}"
if [[ -x /opt/homebrew/bin/python3 ]]; then
  /opt/homebrew/bin/python3 workbench.py open
else
  python3 workbench.py open
fi
if [ $? -ne 0 ]; then
  echo '启动未完成，请查看上面的错误。按回车关闭。'
  read
fi
