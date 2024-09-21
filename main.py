import PyQt6
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtGui import QIcon
import sys

app = QApplication(sys.argv)
mainwindow = QWidget()
mainwindow.setWindowTitle("AUR store")
mainwindow.setWindowIcon(QIcon("images/archicon.png"))
mainwindow.setMinimumSize(500, 500)
mainwindow.show()




app.exec()

