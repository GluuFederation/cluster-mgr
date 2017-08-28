"""removed topology flag from appconfig

Revision ID: 4e32059aca93
Revises: 654790df8aa4
Create Date: 2017-08-23 21:29:47.612191

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4e32059aca93'
down_revision = '654790df8aa4'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("appconfig") as batch_op:
        batch_op.drop_column('topology')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('appconfig', sa.Column('topology', sa.VARCHAR(length=30), nullable=True))
    # ### end Alembic commands ###
